# -*- coding: utf-8 -*-
from sys import version

from offregister_fab_utils.misc import get_user_group_tuples

if version[0] == "2":
    try:
        from cString import StringIO
    except ImportError:
        from StringIO import StringIO
    from urlparse import urlparse
else:
    from io import StringIO
    from urllib import parse as urlparse

from functools import partial
from json import dump, dumps, load
from os import path

import offregister_rabbitmq.ubuntu as rabbitmq
from fabric.api import run
from fabric.context_managers import settings
from fabric.operations import prompt
from offregister_app_push.ubuntu import build_node_app
from offregister_fab_utils import macos
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.fs import cmd_avail
from offregister_fab_utils.git import clone_or_update
from offregister_fab_utils.ubuntu import systemd
from offregister_fab_utils.ubuntu.systemd import restart_systemd
from offregister_postgres import ubuntu as postgres
from offregister_postgres.utils import get_postgres_params
from offregister_python.ubuntu import install_venv0
from offutils import generate_random_alphanum
from patchwork.files import append, exists
from pkg_resources import resource_filename

taiga_dir = partial(
    path.join,
    path.dirname(resource_filename("offregister_taiga", "__init__.py")),
    "data",
)


def install_python_taiga_deps(virtual_env):
    uname = c.run("uname -v").stdout.rstrip()

    is_ubuntu = "Ubuntu" in uname

    if is_ubuntu:
        apt_depends(
            c,
            "build-essential",
            "binutils-doc",
            "autoconf",
            "flex",
            "bison",
            "libjpeg-dev",
            "libfreetype6-dev",
            "zlib1g-dev",
            "libzmq3-dev",
            "libgdbm-dev",
            "libncurses5-dev",
            "automake",
            "libtool",
            "libffi-dev",
            "curl",
            "git",
            "tmux",
            "gettext",
            "libxml2-dev",
            "libxslt1-dev",
            "libssl-dev",
            "libffi-dev",
            "libffi6",
        )
    elif uname.startswith("Darwin"):
        c.run("brew install libxml2 libxslt")
    else:
        raise NotImplementedError(uname)

    c.sudo("mkdir -p {virtual_env}".format(virtual_env=virtual_env))
    group_user = c.run("""printf '%s:%s' "$USER" $(id -gn)""", hide=True)
    c.sudo(
        "chown -R {group_user} {virtual_env}".format(
            group_user=group_user, virtual_env=virtual_env
        )
    )

    pip_version = "19.2.3"

    if is_ubuntu:
        install_venv0(python3=True, virtual_env=virtual_env, pip_version=pip_version)
    else:
        c.run('python3 -m venv "{virtual_env}" '.format(virtual_env=virtual_env))

    env = dict(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env))
    with c.cd("taiga-back"):
        # c.run("sed -i '0,/lxml==3.5.0b1/s//lxml==3.5.0/' requirements.txt")
        c.run("pip3 install -U pip", env=env)
        c.run("pip3 --version; python3 --version", env=env)

        if not is_ubuntu:
            c.run("STATIC_DEPS=true pip3 install lxml", env=env)

        c.run("pip3 install -U setuptools", env=env)
        c.run("pip3 install --no-cache-dir cffi", env=env)
        c.run("pip3 install --no-cache-dir cairocffi", env=env)
        c.run("pip3 install -r requirements.txt", env=env)
    return virtual_env


def _migrate(
    virtual_env, taiga_root, skip_migrate, sample_data, remote_user, database_uri
):
    if skip_migrate:
        return virtual_env

    env = dict(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env))
    with c.cd("{taiga_root}/taiga-back".format(taiga_root=taiga_root)):
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "mydatabase",
                "USER": "mydatabaseuser",
                "PASSWORD": "mypassword",
                "HOST": "127.0.0.1",
                "PORT": "5432",
            }
        }

        if database_uri:
            parsed_connection_str = urlparse(database_uri)

            # TODO: Put this all back in offregister-postgres
            params = get_postgres_params(parsed_connection_str)

            postgres = partial(c.sudo, user="postgres", env=env)

            prompt(
                'Have you set the password to "{database_uri}"'.format(
                    database_uri=database_uri
                )
            )

            if (
                postgres(
                    """[ -f ~/.bash_profile ] && source ~/.bash_profile ; psql -t -c '\l' "{database_uri}" | grep -qF taiga""".format(
                        database_uri=database_uri
                    ),
                    warn=True,
                ).exited
                != 0
            ):
                with settings(prompts={"Password: ": parsed_connection_str.password}):
                    postgres(
                        "[ -f ~/.bash_profile ] && source ~/.bash_profile ; createdb {params} {dbname}".format(
                            params=params, dbname="taiga"
                        )
                    )
            # ENDTODO

            DATABASES["default"]["NAME"] = "taiga"
            DATABASES["default"]["USER"] = parsed_connection_str.username
            DATABASES["default"]["PASSWORD"] = parsed_connection_str.password
            DATABASES["default"]["HOST"] = parsed_connection_str.hostname
            DATABASES["default"]["PORT"] = (
                parsed_connection_str.port or DATABASES["default"]["PORT"]
            )

            # TODO: Use my Django settings.py parser/emitter
            append(
                c,
                c.run,
                "settings/local.py",
                "DATABASES = {}".format(dumps(DATABASES, sort_keys=True)),
            )

        c.run("python3 manage.py migrate --noinput", env=env)
        c.run("python3 manage.py compilemessages", env=env)
        c.run("python3 manage.py collectstatic --noinput", env=env)

        c.run("python3 manage.py loaddata initial_user", env=env)
        c.run("python3 manage.py loaddata initial_project_templates", env=env)

        if sample_data:
            c.run("python3 manage.py sample_data", env=env)
        c.run("python3 manage.py rebuild_timeline --purge", env=env)

    return virtual_env


def _install_frontend(c, taiga_root=None, **kwargs):
    c.sudo("mkdir -p {root}/logs".format(root=taiga_root))
    group_user = c.run("""printf '%s:%s' "$USER" $(id -gn)""", hide=True).stdout
    c.sudo(
        "chown -R {group_user} {root}".format(group_user=group_user, root=taiga_root)
    )

    with c.cd(taiga_root):
        clone_or_update(team="taigaio", repo="taiga-front")
        # Compile it here if you prefer
        if not exists(c, runner=c.run, path="taiga-front/dist"):
            clone_or_update(team="taigaio", repo="taiga-front-dist")
            c.run(
                "ln -s {root}/taiga-front-dist/dist {root}/taiga-front/dist".format(
                    root=taiga_root
                )
            )
            c.run(
                "ln -s {root}/taiga-front-dist/dist/conf.example.json {root}/taiga-front/dist/conf.json".format(
                    root=taiga_root
                )
            )

    if not kwargs.get("skip_nginx"):
        c.sudo("mkdir -p /etc/nginx/sites-enabled")

        upload_template_fmt(
            c,
            taiga_dir("taiga.nginx.conf"),
            "/etc/nginx/sites-enabled/{server_name}.conf".format(
                server_name=kwargs["SERVER_NAME"]
            ),
            context={
                "TAIGA_ROOT": taiga_root,
                "LISTEN_PORT": kwargs["LISTEN_PORT"],
                "SERVER_NAME": kwargs["SERVER_NAME"],
            },
            use_sudo=True,
        )


def _install_backend(
    taiga_root,
    remote_user,
    circus_virtual_env,
    virtual_env,
    database=True,
    database_uri="",
):
    # apt_depends(c, 'circus')
    uname = c.run("uname -v").stdout.rstrip()
    is_ubuntu = "Ubuntu" in uname
    home = c.run("echo $HOME").stdout.rstrip()

    if database:
        if is_ubuntu:
            postgres.install0()

        user = {
            "user": "taiga_user",
            "password": generate_random_alphanum(16),
            "dbname": "taiga_db",
        }
        database_uri = "postgres://{user}:{password}@localhost/{dbname}".format(**user)

        created = postgres.setup_users(create=(user,))
        assert created is not None
    elif not database_uri:
        raise ValueError("Must create database or provide database_uri")

    with c.cd(taiga_root):
        clone_or_update(team="taigaio", repo="taiga-back")
        install_python_taiga_deps(virtual_env)

    # UWSGI
    env = dict(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env))
    c.run("pip3 install uwsgi", env=env)

    if not exists(c, runner=c.run, path="/etc/systemd/system"):
        raise NotImplementedError("Non SystemD platforms")

    if (
        c.run(
            "id {remote_user}".format(remote_user=remote_user), warn=True, hide=True
        ).exited
        != 0
    ):
        c.sudo(
            'adduser {remote_user} --disabled-password --quiet --gecos ""'.format(
                remote_user=remote_user
            )
        )
    (uid, user), (gid, group) = get_user_group_tuples(remote_user)

    upload_template_fmt(
        c,
        taiga_dir("uwsgi.service"),
        "/etc/systemd/system/taiga-uwsgi.service",
        context={
            "USER": user,
            "GROUP": group,
            "PORT": 8001,
            "TAIGA_BACK": "{}/taiga-back".format(taiga_root),
            "UID": uid,
            "GID": gid,
            "VENV": virtual_env,
        },
        use_sudo=True,
    )
    restart_systemd("taiga-uwsgi")

    return virtual_env, database_uri

    # return _setup_circus(circus_virtual_env, database_uri, home, is_ubuntu, remote_user, taiga_root, uname)


def _setup_circus(
    circus_virtual_env,
    taiga_virtual_env,
    database_uri,
    home,
    is_ubuntu,
    remote_user,
    taiga_root,
    uname,
):
    c.sudo(
        "mkdir -p {circus_virtual_env}".format(circus_virtual_env=circus_virtual_env)
    )
    group_user = c.run("""printf '%s:%s' "$USER" $(id -gn)""", hide=True).stdout
    c.sudo(
        "chown -R {group_user} {circus_virtual_env}".format(
            group_user=group_user, circus_virtual_env=circus_virtual_env
        )
    )
    if is_ubuntu:
        install_venv0(python3=True, virtual_env=circus_virtual_env)
    else:
        c.run('python3 -m venv "{virtual_env}"'.format(virtual_env=circus_virtual_env))
    env = dict(
        VIRTUAL_ENV=circus_virtual_env, PATH="{}/bin:$PATH".format(circus_virtual_env)
    )
    c.run("python -m pip install circus", env=env)
    conf_dir = "/etc/circus/conf.d"  # '/'.join((taiga_root, 'config'))
    c.sudo("mkdir -p {conf_dir}".format(conf_dir=conf_dir))
    py_ver = c.run(
        "{virtual_env}/bin/python --version".format(virtual_env=taiga_virtual_env)
    ).partition(" ")[2][:3]
    upload_template_fmt(
        c,
        taiga_dir("circus.ini"),
        "{conf_dir}/".format(conf_dir=conf_dir),
        context={
            "HOME": taiga_root,
            "USER": remote_user,
            "VENV": taiga_virtual_env,
            "PYTHON_VERSION": py_ver,
        },
        use_sudo=True,
    )
    circusd_context = {"CONF_DIR": conf_dir, "CIRCUS_VENV": circus_virtual_env}
    if uname.startswith("Darwin"):
        upload_template_fmt(
            c,
            taiga_dir("circusd.launchd.xml"),
            "{home}/Library/LaunchAgents/io.readthedocs.circus.plist".format(home=home),
            context=circusd_context,
        )
    elif exists(c, runner=c.run, path="/etc/systemd/system"):
        upload_template_fmt(
            c,
            taiga_dir("circusd.service"),
            "/etc/systemd/system/",
            context=circusd_context,
            use_sudo=True,
        )
    else:
        upload_template_fmt(
            c,
            taiga_dir("circusd.conf"),
            "/etc/init/",
            context=circusd_context,
            use_sudo=True,
        )
    return circus_virtual_env, database_uri


def _install_events(taiga_root):
    uname = c.run("uname -v").stdout.rstrip()
    is_ubuntu = "Ubuntu" in uname

    if not cmd_avail(c, "rabbitmqctl"):
        if is_ubuntu:
            rabbitmq.install0()
        elif "Darwin" in uname:
            c.run("brew install rabbitmq")
            if not cmd_avail(c, "rabbitmqctl"):
                append(
                    c, c.run, "$HOME/.bash_profile", "export PATH=/usr/local/sbin:$PATH"
                )
                c.run("logout")
            c.run("brew services start rabbitmq")
        else:
            raise NotImplementedError(uname)
    user = "taiga"

    if (
        c.sudo(
            "rabbitmqctl list_users | grep -q {user}".format(user=user), warn=True
        ).exited
        == 0
    ):
        return

    password = rabbitmq.create_user1(rmq_user=user, rmq_vhost=user)

    rmq_uri = "amqp://{user}:{password}@localhost:5672/{user}".format(
        user=user, password=password
    )

    event_root = "{taiga_root}/taiga-events".format(taiga_root=taiga_root)
    clone_or_update(
        c, team="taigaio", repo="taiga-events", branch="master", to_dir=event_root
    )
    with c.cd(event_root):
        build_node_app(
            kwargs=dict(npm_global_packages=("coffeescript",), node_version="lts"),
            run_cmd=run,
        )
    upload_template_fmt(
        c, taiga_dir("config.json"), event_root, context={"RMQ_URI": rmq_uri}
    )

    user = c.run("echo $USER", hide=True).stdout.rstrip()
    if cmd_avail(c, "systemctl"):
        return systemd.install_upgrade_service(
            c,
            service_name="taiga_events",
            context={
                "User": user,
                "Group": c.run("id -gn").stdout or user,
                "Environments": "",
                "WorkingDirectory": event_root,
                "ExecStart": "/bin/bash -c 'PATH=/home/{user}/n/bin:$PATH /home/{user}/n/bin/coffee index.coffee'".format(
                    user=user
                ),
            },
        )
    elif uname.startswith("Darwin"):
        return macos.install_upgrade_service(
            "io.taiga.events",
            context={
                "PROGRAM": "/bin/bash -c 'PATH=/home/{user}/n/bin:$PATH /home/{user}/n/bin/coffee index.coffee'"
            },
        )
    raise NotImplementedError(uname)


def _replace_configs(
    c,
    server_name,
    listen_port,
    taiga_root,
    email,
    public_register_enabled,
    database_uri,
    force_clean=False,
):
    protocol = "https" if listen_port == 443 else "http"

    fqdn = "{protocol}://{server_name}".format(
        protocol=protocol, server_name=server_name
    )

    # Frontend
    js_conf_dir = "/".join((taiga_root, "taiga-front", "dist", "js"))
    conf_json_fname = "/".join((js_conf_dir, "conf.json"))
    if force_clean:
        c.run("rm -rfv {}".format(conf_json_fname))
    if not exists(c, runner=c.run, path=conf_json_fname):
        c.run("mkdir -p {conf_dir}".format(conf_dir=js_conf_dir))

        with open(taiga_dir("conf.json")) as f:
            conf = load(f)

        conf["api"] = "{fqdn}{path}".format(fqdn=fqdn, path=conf["api"])

        event_config = "{taiga_root}/taiga-events/config.json".format(
            taiga_root=taiga_root
        )
        if exists(c, runner=c.run, path=event_config):
            if not cmd_avail(c, "jq"):
                apt_depends(c, "jq")
            conf["eventsUrl"] = c.run(
                "jq -r .url {event_config}".format(event_config=event_config)
            )
        sio = StringIO()
        dump(conf, sio, indent=4, sort_keys=True)
        c.put(sio, conf_json_fname)

    # Backend
    local_py = "{taiga_root}/taiga-back/settings/local.py".format(taiga_root=taiga_root)
    if force_clean:
        c.run("rm -rfv {}".format(local_py))

    stat = c.run("stat -c'%s' {}".format(local_py), warn=True)

    if stat.exited != 0 or int(stat) == 0:
        context = {
            "FQDN": fqdn,
            "PROTOCOL": protocol,
            "SERVER_NAME": server_name,
            "SECRET_KEY": generate_random_alphanum(52),
            "DEFAULT_FROM_EMAIL": email or "no-reply@example.com",
            "PUBLIC_REGISTER_ENABLED": public_register_enabled,
        }
        mq = c.run(
            "jq -r .url {taiga_root}/taiga-events/config.json".format(
                taiga_root=taiga_root
            ),
            warn=True,
        )

        if not mq.exited != 0 and mq:
            context.update(
                {
                    "EVENTS_PUSH_BACKEND": "taiga.events.backends.rabbitmq.EventsPushBackend",
                    "EVENTS_PUSH_BACKEND_OPTIONS": {"url": mq},
                }
            )

        if database_uri:
            _databases = frozenset(
                (
                    "django.db.backends.postgresql"
                    "django.db.backends.mysql"
                    "django.db.backends.sqlite3"
                    "django.db.backends.oracle"
                )
            )

            parsed_connection_str = urlparse(database_uri)
            params = get_postgres_params(parsed_connection_str)

            postgres = partial(c.sudo, user="postgres", shell_escape=False)

            if (
                postgres(
                    """[ -f ~/.bash_profile ] && source ~/.bash_profile ; psql -t -c '\l' "{database_uri}" | grep -qF taiga""".format(
                        database_uri=database_uri
                    ),
                    warn=True,
                ).exited
                != 0
            ):
                with settings(prompts={"Password: ": parsed_connection_str.password}):
                    postgres(
                        "[ -f ~/.bash_profile ] && source ~/.bash_profile ; createdb {params} {dbname}".format(
                            params="--owner={}".format(params.rpartition("=")[2]),
                            dbname="taiga",
                        )
                    )
            # ENDTODO

            context["DATABASES"] = {
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": parsed_connection_str.path
                    and parsed_connection_str.path[1:]
                    or "taiga",
                    "USER": parsed_connection_str.username,
                    "PASSWORD": parsed_connection_str.password,
                    "HOST": parsed_connection_str.hostname,
                    "PORT": parsed_connection_str.port or 5432,
                }
            }

        upload_template_fmt(c, taiga_dir("local.py"), local_py, context=context)

    stat = c.run("stat -c'%s' {}".format(local_py), warn=True)
    if stat.exited != 0 or int(stat) == 0:
        raise IOError("{} doesn't exist; did you install?".format(local_py))

    cmd = (
        "sed -i -e 's|http://localhost:8000|{fqdn}|g' "
        "-e 's|localhost:8000|{server_name}|g' "
        "-e 's|http|{protocol}|g' "
        "-e 's|httphttp|http|g' "
        "-e 's|https\\+|https|g' ".format(
            fqdn=fqdn, server_name=server_name, protocol=protocol
        )
    )

    # Back
    c.run(
        'for f in {taiga_root}/taiga-back/settings/*; do {cmd} "$f"; done'.format(
            cmd=cmd, taiga_root=taiga_root
        ),
        shell=False,
    )

    # Front
    c.run(
        "{cmd} {taiga_root}/taiga-front/app-loader/app-loader.coffee".format(
            cmd=cmd, taiga_root=taiga_root
        )
    )

    # Front (dist)
    c.run(
        'for f in {taiga_root}/taiga-front-dist/dist/*.json; do {cmd} "$f"; done'.format(
            cmd=cmd, taiga_root=taiga_root
        ),
        shell=False,
    )

    # Everything
    """
    c.run('find {taiga_root} -type f -name .git -prune -o {extra} -exec {cmd} {end}'.format(
        taiga_root=taiga_root,
        extra='-exec grep -Iq . {} \\; -and -print',
        cmd=cmd,
        end='{} \;'
    ), shell_escape=False)
    """
