import os
import sys

from fno import service
from fno.server import serve


def main() -> int:
    port_text = os.environ.get("PORT", "")
    try:
        port = int(port_text)
    except ValueError:
        raise SystemExit("PORT must be set to a valid TCP port")

    data_dir = os.environ.get("FNO_DATA_DIR", "data")
    srv = serve(
        service.Context(data_dir),
        host="0.0.0.0",
        port=port,
        allow_external=True,
    )
    print(
        f"F&O Render web service listening on 0.0.0.0:{port} "
        f"(data in {data_dir})",
        file=sys.stdout,
        flush=True,
    )
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
