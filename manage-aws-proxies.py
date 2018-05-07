#!/usr/bin/env python3

#import paramiko, boto3
import boto3, subprocess, gc, argparse, signal, sys, traceback
from time import sleep
from os import chmod
from datetime import tzinfo, timedelta, datetime
from jinja2 import Environment, FileSystemLoader


class UTC(tzinfo):
    def utcoffset(self, dt):
        return timedelta(0)
    def tzname(self, dt):
        return "UTC"
    def dst(self, dt):
        return timedelta(0)


class Node:
    def __init__(self, aws_instance, ports):
        self.tunnels = list()
        self.aws_instance = aws_instance
        self.ports = ports
        if self.aws_instance['ImageId'] == 'ami-9cc0d5f8': # ssh user for debian images
            self.user = 'admin'
        else:
            self.user = 'ec2-user'

    def update(self, ec2_client):
        self.aws_instance = ec2_client.describe_instances(InstanceIds=[self.aws_instance['InstanceId']])['Reservations'][0]['Instances'][0]

    def create_ssh_tunnels(self, keyfile):
        chmod(keyfile, 0o400)
        for port in self.ports:
            self.tunnels.append(
                subprocess.Popen([
                    'ssh', '-N', '-q',
                    '-o', 'StrictHostKeyChecking=no',
                    '-o', 'UserKnownHostsFile=/dev/null',
                    '-i', keyfile,
                    '-D', '127.0.0.1:'+str(port),
                    '-l', self.user,
                    self.aws_instance['PublicDnsName']
                ])
            )

    def stop_ssh_tunnels(self):
        for tunnel in self.tunnels:
            tunnel.terminate()
            tunnel = None
        self.tunnels.clear()
        return self.ports

    def terminate(self, ec2_client):
        ec2_client.terminate_instances(InstanceIds=[self.aws_instance['InstanceId']])
        self.aws_instance = None
        return self.stop_ssh_tunnels()


class Haproxy:
    def __init__(self, running_instances, templates_dir, haproxy_template, configFile='/haproxy.cfg'):
        self.dir      = templates_dir
        self.template = haproxy_template
        self.config   = configFile

        self.update_conf(running_instances)

        self.process  = subprocess.Popen([
            'haproxy', '-db', '-q',
            '-f', self.config
        ])

    def update_conf(self, running_instances):
        self.env   = Environment(loader=FileSystemLoader(self.dir), trim_blocks=True)
        self.templ = self.env.get_template(self.template)
        output     = self.templ.render(instances=running_instances) 
        with open(self.config, 'w') as f:
            f.write(output)

    def reload(self):
        self.process = subprocess.Popen([
            'haproxy', '-db',
            '-f', self.config,
            '-sf', str(self.process.pid)
        ])

    def stop(self):
        self.process.terminate()

def sigterm_handler(_signo, _stack_frame):
    sys.exit(0)

def main(loop_time, keyfile, ami_keyname, ec2_img, ec2_type, sec_group, templates_dir, haproxy_template, instances_ttl, tunnels_by_instance, required_instances=1):
    existing_instances    = []
    non_running_instances = set()
    running_instances     = set()
    pending_instances     = set()
    avail_ports           = list(range(8080, 8980))

    ec2_client         = boto3.client('ec2')
    ec2_resource       = boto3.resource('ec2')
    haproxy            = Haproxy(running_instances, templates_dir, haproxy_template)

    signal.signal(signal.SIGTERM, sigterm_handler)

    print("Starting main loop")
    try:
        # Check for existing instances on startup
        existing_instances = ec2_client.describe_instances()['Reservations']
        for aws_instance in existing_instances:
            if aws_instance['Instances'][0]['State']['Name'] == 'pending': # TODO, identifier les codes d'erreur
                pending_instances.add(
                    Node(
                        aws_instance['Instances'][0],
                        [avail_ports.pop(0) for x in range(tunnels_by_instance)]
                    )
                )
                continue
    
            if aws_instance['Instances'][0]['State']['Name'] == 'running':
                instance = Node(
                    aws_instance['Instances'][0],
                    [avail_ports.pop(0) for x in range(tunnels_by_instance)]
                )
                running_instances.add(instance)
                instance.create_ssh_tunnels(keyfile)
                continue
    
        haproxy.update_conf(running_instances)
        haproxy.reload()
    
        while True:
            # Update instances status
            for instance in running_instances.union(pending_instances):
                instance.update(ec2_client)
    
            # Set previously pending instances as running
            started = filter(lambda i: i.aws_instance['State']['Name'] == 'running', pending_instances)
            for instance in started:
                print("Changing state of instance", instance.aws_instance['InstanceId'],
                    "from pending to started")
                running_instances.add(instance)
                instance.create_ssh_tunnels(keyfile)
                haproxy.update_conf(running_instances)
                haproxy.reload()
            pending_instances.difference_update(running_instances)
    
            # Check status for running instances
            for instance in running_instances:
                if instance.aws_instance['State']['Name'] != 'running':
                    print(
                        'Warning, Instance {} is in the {} state'.format(
                            instance.aws_instance['InstanceId'],
                            instance.aws_instance['State']['Name']
                        )
                    )
                    avail_ports.extend(instance.terminate(ec2_client))
                    non_running_instances.add(instance)
                    continue
                try: # Allow to update process return code. Is there a cleaner way ?
                    for tunnel in instance.tunnels:
                        tunnel.communicate(timeout=0.5)
                except:
                    True
                for tunnel in instance.tunnels:
                    if tunnel.returncode is not None:
                        instance.stop_ssh_tunnels()
                        instance.create_ssh_tunnels(keyfile)
                        break
            running_instances.difference_update(non_running_instances)
            if len(non_running_instances) > 0:
                haproxy.update_conf(running_instances)
                haproxy.reload()
                non_running_instances = set()
    
            # Workaround to check that we don't have "rogue" instances
            dst_instances_count = 0
            for aws_dst_instance in ec2_client.describe_instances()['Reservations']:
                if (aws_dst_instance['Instances'][0]['State']['Name'] == 'pending' or
                    aws_dst_instance['Instances'][0]['State']['Name'] == 'running'):

                    dst_instances_count += 1

            if dst_instances_count > len(running_instances) + len(pending_instances):
                print("Something nasty has appenned. We die")
                break

            # Delete older instance if TTL is reached and we have enough instances
            if len(running_instances) > required_instances:
                older_instance = min(running_instances, key=lambda i: i.aws_instance['LaunchTime'])
                if (datetime.now(UTC()) - older_instance.aws_instance['LaunchTime']).total_seconds() > instances_ttl:
                    print("Removing older instance", older_instance.aws_instance['InstanceId'])
                    running_instances.remove(older_instance)
                    haproxy.update_conf(running_instances)
                    haproxy.reload()
                    avail_ports.extend(older_instance.terminate(ec2_client))
    
            # Create new instance if needed
            if len(running_instances) + len(pending_instances) <= required_instances:
                print("Creating new instance to reach targeted number")
                try:
                    created = ec2_resource.create_instances(
                        ImageId=ec2_img,
                        KeyName=ami_keyname,
                        SecurityGroupIds=[
                            sec_group
                        ],
                        InstanceType=ec2_type,
                        MinCount=1, MaxCount=1
                    )
                    print("instance created, sleeping one second, then adding to local set")
                    sleep(1)
                    pending_instances.add(
                        Node(
                            ec2_client.describe_instances(
                                InstanceIds=[
                                    created[0].id
                                ]
                            )['Reservations'][0]['Instances'][0],
                            [avail_ports.pop(0) for x in range(tunnels_by_instance)]
                        )
                    )
                    print("instance correctly added:", created)
                except Exception as err:
                    print("Error while creating aws Instance:", created)
                    traceback.print_tb(err.__traceback__)
 
            sleep(loop_time)
            gc.collect()
    finally:
        for instance in running_instances:
            instance.stop_ssh_tunnels()
        haproxy.stop()
        


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--templates_dir",       help="Directory where are stored jinja2 templates", required=True)
    parser.add_argument("--haproxy_template",    help="HAProxy template used by jinja2", required=True)
    parser.add_argument("--keyfile",             help="ssh key used to connect to AWS ec2 instances", required=True)
    parser.add_argument("--ami_keyname",         help="ami key used to connect to AWS API", required=True)
    parser.add_argument("--ec2_type",            help="AWS instance type used for instances", required=True)
    parser.add_argument("--ec2_img",             help="AWS ec2 image used for booting instances", required=True)
    parser.add_argument("--sec_group",           help="AWS security group used for instances", required=True)

    parser.add_argument("--required_instances",  help="Number of instances to run simulteanously", required=True, type=int)
    parser.add_argument("--instances_ttl",       help="Time after instances will be remplaced by new ones", required=True, type=int)
    parser.add_argument("--tunnels_by_instance", help="Number of ssh tunnels established by aws instances", required=True, type=int)

    parser.add_argument("--loop_time",           help="Time to wait between two loops iterations", default=60, type=int)

    args = parser.parse_args()

    # Args Examples
    #templates_dir    = '/templates'
    #haproxy_template = 'haproxy.cfg.tmpl'
    #keyfile = '/ssh-key/aws-proxies.pem'
    #ami_keyname = 'aws-proxies'
    #ec2_type    = 't2.micro'
    #ec2_img  = 'ami-9cc0d5f8'
    #sec_group   = 'sg-40a33c29'

    #required_instances = 2
    #instances_ttl     = 900

    main(
        args.loop_time,
        args.keyfile,
        args.ami_keyname,
        args.ec2_img,
        args.ec2_type,
        args.sec_group,
        args.templates_dir,
        args.haproxy_template,
        args.instances_ttl,
        args.tunnels_by_instance,
        required_instances=args.required_instances
    )
