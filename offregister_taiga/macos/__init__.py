from offregister_taiga.utils import (
    _install_backend,
    _install_events,
    _install_frontend,
    _migrate,
    _replace_configs,
)


def install0(*args, **kwargs):
    kwargs.setdefault("remote_user", "ubuntu")
    kwargs.setdefault("virtual_env", "/opt/venvs/taiga")
    kwargs.setdefault("circus_virtual_env", "/opt/venvs/circus")
    kwargs.setdefault("EMAIL", "no-reply@example.com")
    kwargs.setdefault("public_register_enabled", True)
    kwargs.setdefault("TAIGA_ROOT", c.run("printf $HOME", hide=True).stdout)
    kwargs.setdefault("skip_migrate", False)

    _install_frontend(taiga_root=kwargs["TAIGA_ROOT"], **kwargs)
    circus_virtual_env, database_uri = _install_backend(
        taiga_root=kwargs["TAIGA_ROOT"],
        remote_user=kwargs["remote_user"],
        circus_virtual_env=kwargs["circus_virtual_env"],
        virtual_env=kwargs["virtual_env"],
        database_uri=kwargs["RDBMS_URI"],
        database=True,
    )
    _install_events(taiga_root=kwargs["TAIGA_ROOT"])

    _replace_configs(
        taiga_root=kwargs["TAIGA_ROOT"],
        server_name=kwargs["SERVER_NAME"],
        listen_port=kwargs["LISTEN_PORT"],
        email=kwargs["EMAIL"],
        public_register_enabled=kwargs["public_register_enabled"],
        database_uri=database_uri,
        force_clean=False,
    )
    _migrate(
        virtual_env=kwargs["virtual_env"],
        taiga_root=kwargs["TAIGA_ROOT"],
        skip_migrate=kwargs["skip_migrate"],
        remote_user=kwargs["remote_user"],
        sample_data=kwargs.get("sample_data"),
        database_uri=kwargs["RDBMS_URI"],
    )
