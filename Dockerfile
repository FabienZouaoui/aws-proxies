FROM		alpine:latest
MAINTAINER	Fabien Zouaoui <fzo@sirdata.fr>
LABEL		Description="Base alpine with haproxy and stuff to forward requests to socks proxy instances"

RUN apk update && \
    apk add ca-certificates openssl haproxy python3 groff less openssh-client py3-jinja2 && \
    update-ca-certificates && \
    rm -f /var/cache/apk/*
RUN pip3 install --upgrade pip && pip3 install awscli boto3

RUN mkdir /templates
COPY manage-aws-proxies.py /manage-aws-proxies.py
COPY haproxy.cfg.tmpl /templates/haproxy.cfg.tmpl

#USER daemon

ENTRYPOINT ["/bin/sh"]
