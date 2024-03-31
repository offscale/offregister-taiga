# -*- coding: utf-8 -*-
from offregister_docker import ubuntu as docker
from offutils import generate_random_alphanum


def install0(c, *args, **kwargs):
    docker.install_docker0()
    docker.install_docker_user1()
    docker.test_docker2()


def setup_taiga1(c, SERVER_NAME, *args, **kwargs):
    password = kwargs.get("postgres_password", generate_random_alphanum(15))
    c.run("echo {password} > $(mktemp postgres_passwordXXX)".format(password=password))

    c.sudo("mkdir -p /usr/src/taiga-back/media")

    c.run(
        "docker run --name taiga-postgres -d -e POSTGRES_PASSWORD={password} postgres".format(
            password=password
        )
    )
    c.run("docker run --name taiga-redis -d redis:3")
    c.run("docker run --name taiga-rabbit -d --hostname taiga rabbitmq:3")
    c.run("docker run --name taiga-celery -d --link taiga-rabbit:rabbit celery")
    c.run(
        "docker run --name taiga-events -d --link taiga-rabbit:rabbit benhutchins/taiga-events"
    )

    # sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout /etc/ssl/private/nginx-selfsigned.key -out /etc/ssl/certs/nginx-selfsigned.crt
    # sudo openssl dhparam -out /etc/ssl/certs/dhparam.pem 2048

    c.run(
        "docker run -itd \
    --name taiga \
    --link taiga-postgres:postgres \
    --link taiga-redis:redis \
    --link taiga-rabbit:rabbit \
    --link taiga-events:events \
    -e TAIGA_SSL=True \
    -e TAIGA_HOSTNAME={SERVER_NAME} \
    -e DB_PASS={password} \
    -e TAIGA_DB_PASSWORD={password} \
    -v $(pwd)/ssl.crt:/etc/ssl/certs/nginx-selfsigned.crt:ro \
    -v $(pwd)/ssl.key:/etc/ssl/private/nginx-selfsigned.key:ro \
    -p 443:443 \
    -v /media:/usr/src/taiga-back/media \
    benhutchins/taiga".format(
            SERVER_NAME=SERVER_NAME, password=password
        )
    )
