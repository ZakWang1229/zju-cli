"""Audio task body. The execution context owns all persistence."""
from pathlib import Path

from ..errors import LocalFileError, ZjuError
from .audio_jobs import export_audio


class AudioTask:
    def __init__(self, service_factory):
        self.service_factory = service_factory

    def __call__(self, context, parameters, recovery):
        context.check()
        source = context.artifact_path("audio" + Path(parameters["output_path"]).suffix.lower())
        metadata = (recovery or {}).get("audio") or parameters.get("result")
        if metadata:
            try:
                complete = source.stat().st_size == metadata["bytes"] and metadata["bytes"] > 0
            except (OSError, KeyError):
                complete = False
            if not complete:
                metadata = None
        if metadata is None:
            context.report(stage="authenticating")
            service = self.service_factory(parameters)
            metadata = service.audio(parameters["sub_id"], parameters["course_id"], source,
                overwrite=True, ffmpeg_bin=parameters["ffmpeg_bin"], timeout=parameters["timeout"],
                on_media_progress=lambda progress: context.report(progress, stage="extracting"),
                should_stop=context.should_stop)
            context.check()
            if (not source.is_file() or metadata.get("bytes", 0) <= 0
                    or source.stat().st_size != metadata["bytes"]):
                raise LocalFileError("内部音频结果不完整")
            metadata["written"] = True
            context.checkpoint({"audio": metadata})
        metadata = dict(metadata)
        context.report(stage="writing")
        context.check()
        try:
            export_audio(source, Path(parameters["output_path"]),
                         overwrite=parameters["overwrite"], check_stop=context.check)
        except LocalFileError as exc:
            metadata.update(output_error=str(exc), requested_output_path=parameters["output_path"])
        else:
            metadata["out_path"] = parameters["output_path"]
        return metadata
