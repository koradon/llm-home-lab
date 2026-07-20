import tempfile

from llm_home_lab.registry.external_load import ExternalLoadProbe


def new_registry_db_path() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def inert_external_load_probe() -> ExternalLoadProbe:
    async def create_subprocess(*args, **kwargs):
        raise FileNotFoundError("lms not installed in test environment")

    return ExternalLoadProbe(create_subprocess=create_subprocess)
