"""One isolated local block. Full checksums in untimed warmup, row checks per run."""

import gc
import json
import resource
import sys

import pandas as pd
import pyarrow as pa

from benchmarks.sweep_workloads import consume_local


def main():
    config = json.loads(sys.argv[1])
    pa.set_cpu_count(config["threads"])
    pa.set_io_thread_count(config["threads"])
    pd.set_option("compute.use_numexpr", False)
    warmup = consume_local(config, validate=True)
    if config.get("expected") is not None and warmup["checksum"] != config["expected"]:
        raise ValueError("Full-output warmup checksum differs from common reference")
    print(json.dumps({"type": "warmup", **warmup}), flush=True)
    for run in range(1, config["runs"] + 1):
        gc.collect()
        result = consume_local(config, validate=False)
        if result["output_rows"] != warmup["output_rows"]:
            raise ValueError("Measured output row count differs from validated warmup")
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result.update(
            type="measurement",
            run=run,
            validation="row-count; full checksum in block warmup",
            block_peak_rss_bytes=int(peak if sys.platform == "darwin" else peak * 1024),
        )
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
