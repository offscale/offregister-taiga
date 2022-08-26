from sys import version

from offutils.util import iteritems

if version[0] == "2":
    try:
        from cStringIO import StringIO
    except ImportError:
        from StringIO import StringIO
else:
    from io import StringIO

from functools import partial
from json import dump, load
from os import path

from fabric.contrib.files import append, exists
from nginx_parse_emit.utils import DollarTemplate
from offregister_fab_utils.apt import apt_depends
from offregister_fab_utils.ubuntu.systemd import restart_systemd
from pkg_resources import resource_filename

from offregister_taiga.utils import (
    _install_backend,
    _install_events,
    _install_frontend,
    _replace_configs,
    _setup_circus,
)

taiga_dir = partial(
    path.join,
    path.dirname(resource_filename("offregister_taiga", "__init__.py")),
    "data",
)


def install0(*args, **kwargs):
    if (
        c.run("dpkg -s {package}".format(package="nginx"), hide=True, warn=True).exited
        != 0
    ):
        apt_depends(c, "curl")
        c.sudo("curl https://nginx.org/keys/nginx_signing.key | apt-key add -")
        codename = c.run("lsb_release -cs")
        append(
            "/etc/apt/sources.list",
            "deb http://nginx.org/packages/ubuntu/ {codename} nginx".format(
                codename=codename
            ),
            use_sudo=True,
        )

        apt_depends(c, "nginx")

    apt_depends(c, "git")
    _install_frontend(taiga_root=kwargs.get("TAIGA_ROOT"), **kwargs)
    _, database_uri = _install_backend(
        taiga_root=kwargs.get("TAIGA_ROOT", "/var/www/taiga"),
        remote_user=kwargs.get(
            "remote_user", "taiga"  # ('taiga_user', gen_random_str(15), 'taiga')
        ),
        circus_virtual_env=kwargs.get("CIRCUS_VIRTUALENV", "/opt/venvs/circus"),
        virtual_env=kwargs.get("TAIGA_VIRTUALENV", "/opt/venvs/taiga")
        # server_name=kwargs['SERVER_NAME'], skip_migrate=kwargs.get('skip_migrate', False)
    )
    _install_events(taiga_root=kwargs.get("TAIGA_ROOT"))

    _replace_configs(
        taiga_root=kwargs.get("TAIGA_ROOT"),
        server_name=kwargs["SERVER_NAME"],
        listen_port=kwargs["LISTEN_PORT"],
        email=kwargs.get("EMAIL"),
        public_register_enabled=kwargs.get("public_register_enabled", True),
        database_uri=database_uri,
        force_clean=False,
    )

    return "installed taiga"


def serve1(*args, **kwargs):
    restart_systemd("nginx")
    # restart_systemd('circusd')
    restart_systemd("taiga-uwsgi")
    return "served taiga"


def reconfigure2(*args, **kwargs):
    kwargs.setdefault("remote_user", "ubuntu")
    taiga_root = kwargs.get("TAIGA_ROOT", c.run("printf $HOME", hide=True).stdout)
    uname = c.run("uname -v").stdout.rstrip()
    is_ubuntu = "Ubuntu" in uname

    github = "GITHUB" in kwargs and "client_id" in kwargs["GITHUB"]
    virtual_env = "/opt/venvs/taiga"

    # Frontend
    front_config = "{taiga_root}/taiga-front-dist/dist/js/conf.json".format(
        taiga_root=taiga_root
    )
    if not exists(c, runner=c.run, path=front_config):
        raise IOError("{} doesn't exist; did you install?".format(front_config))

    sio = StringIO()
    c.get(front_config, sio)
    sio.seek(0)
    conf = load(sio)

    if github:
        kwargs["TAIGA_FRONT_gitHubClientId"] = kwargs["GITHUB"]["client_id"]
        apt_depends(c, "subversion")

        dist = "{taiga_root}/taiga-front-dist/dist".format(taiga_root=taiga_root)
        if not exists(
            c, runner=c.run, path="{dist}/plugins/github-auth".format(dist=dist)
        ):
            env = dict(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env))
            with c.cd(dist):
                c.run("mkdir -p plugins", env=env)
                c.run(
                    '''svn export "https://github.com/taigaio/taiga-contrib-github-auth/tags/$(pip show taiga-contrib-github-auth | awk '/^Version: /{print $2}')/front/dist"  "github-auth"''',
                    env=env,
                )

        conf["contribPlugins"].append("/plugins/github-auth/github-auth.json")

    conf.update(
        {
            k[len("TAIGA_FRONT_") :]: v
            for k, v in iteritems(kwargs)
            if k.startswith("TAIGA_FRONT")
        }
    )

    sio = StringIO()
    dump(conf, sio, indent=4, sort_keys=True)
    c.put(sio, front_config)

    # Backend
    back_config = "{taiga_root}/taiga-back/settings/local.py".format(
        taiga_root=taiga_root
    )
    stat = c.run("stat -c'%s' {}".format(back_config), warn=True)

    if stat.exited != 0 or int(stat) == 0:
        raise IOError("{} doesn't exist; did you install?".format(back_config))

    if github:
        env = dict(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env))
        c.run("pip3 install taiga-contrib-github-auth", env=env)

        c.run(
            DollarTemplate(
                """sed -i -n 
            -e '/^IMPORTERS\[\"github\"\]/!p'
            -e '$aIMPORTERS["github"] = { "active": True, "client_id": "$client_id","client_secret": "$client_secret"}'
            -e '/^GITHUB_API_CLIENT_ID/!p'
            -e '$aGITHUB_API_CLIENT_ID = "$client_id"'
            -e '/^GITHUB_API_URL/!p' -e '$aGITHUB_API_URL = "$api_url"
            -e '/^GITHUB_URL/!p' -e '$aGITHUB_URL = "$url"'
             $back_config"""
            ).safe_substitute(
                client_id=kwargs["GITHUB"]["client_id"],
                client_secret=kwargs["GITHUB"]["client_secret"],
                api_url=kwargs["GITHUB"].get("api_url", "https://api.github.com/"),
                url=kwargs["GITHUB"].get("url", "https://github.com/"),
                back_config=back_config,
            )
        )

        install = 'INSTALLED_APPS += ["taiga_contrib_github_auth"]'
        if (
            c.run(
                "grep -qF '{install}' {back_config}".format(
                    install=install, back_config=back_config
                ),
                warn=True,
            ).exited
            != 0
        ):
            append(back_config, install)

    """
    sio = StringIO()
    c.get(back_config, sio)

    back_conf_s = sio.read()
    # Edit here

    sio = StringIO()
    sio.write(back_conf_s)
    sio.seek(0)
    c.put(sio, back_config)
    """

    if not exists(c, runner=c.run, path="/etc/systemd/system/circusd.service"):
        circus_virtual_env = kwargs.get("CIRCUS_VIRTUALENV", "/opt/venvs/circus")
        env = dict(VIRTUAL_ENV=virtual_env, PATH="{}/bin:$PATH".format(virtual_env))
        if (
            c.run(
                """python -c 'import pkgutil; exit(0 if pkgutil.find_loader("django_settings_cli") else 2)' """,
                warn=True,
                hide=True,
                env=env,
            ).exited
            != 0
        ):
            c.run(
                "python -m pip install"
                " https://api.github.com/repos/offscale/django-settings-cli/zipball#egg=django_settings_cli",
                env=env,
            )
        database_uri = "python -m django_settings_cli parse .DATABASES.default {}{}".format(
            taiga_root,
            """taiga-back/settings/local.py -f
             '{ENGINE[ENGINE.rfind(".")+1:]}://{USER}@{HOST or "localhost"}:{PORT or 5432}/{NAME}' -r""",
        )
        _setup_circus(
            home=c.run("echo $HOME", hide=True).stdout.rstrip(),
            circus_virtual_env=circus_virtual_env,
            taiga_virtual_env=kwargs.get("TAIGA_VIRTUALENV", "/opt/venvs/taiga"),
            remote_user=kwargs["remote_user"],
            database_uri=database_uri,
            taiga_root=taiga_root,
            is_ubuntu=is_ubuntu,
            uname=uname,
        )
        return restart_systemd("circusd")
