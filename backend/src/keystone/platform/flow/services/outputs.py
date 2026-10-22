from keystone.core.errors import ServiceError
from keystone.platform.files.services.references import retain_registered_output
from keystone.platform.flow.schemas.files import file_ids


async def retain_outputs(session, run, result):
    for identifier in sorted(file_ids(result, run.definition_snapshot["output_file_fields"])):
        try:
            await retain_registered_output(session, f"flow:{run.id}", identifier)
        except ServiceError as error:
            if error.code == "file_reference_invalid":
                raise ServiceError(
                    409, "flow_invalid_output", "输出文件没有本次运行的有效使用记录"
                ) from None
            raise
