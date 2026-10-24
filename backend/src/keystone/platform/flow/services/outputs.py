from keystone.core.errors import ServiceError
from keystone.platform.files.services.references import retain_registered_output
from keystone.platform.flow.schemas.files import (
    compile_file_paths,
    extract_file_ids,
    extract_legacy_file_ids,
)
from keystone.platform.flow.schemas.snapshots import read_snapshot


async def retain_outputs(session, run, result):
    snapshot = read_snapshot(run.definition_snapshot)
    if "format_version" not in snapshot:
        identifiers = extract_legacy_file_ids(result, snapshot["output_file_fields"])
    else:
        paths = compile_file_paths(snapshot["output_schema"], snapshot["output_file_fields"])
        identifiers = extract_file_ids(result, paths)
    for identifier in sorted(identifiers):
        try:
            await retain_registered_output(session, f"flow:{run.id}", identifier)
        except ServiceError as error:
            if error.code == "file_reference_invalid":
                raise ServiceError(
                    409, "flow_invalid_output", "输出文件没有本次运行的有效使用记录"
                ) from None
            raise
