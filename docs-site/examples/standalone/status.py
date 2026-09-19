"""Read the latest offline status without treating it as signed execution evidence."""

import argparse
import asyncio
import json

import nats
from nats.js.errors import NotFoundError


async def run(args: argparse.Namespace) -> None:
    connection = await nats.connect(args.nats_url)
    try:
        jetstream = connection.jetstream()
        stream = await jetstream.find_stream_name_by_subject(args.subject)
        async with asyncio.timeout(args.timeout):
            while True:
                try:
                    message = await jetstream.get_last_msg(stream, args.subject)
                    document = json.loads(message.data)
                    payload = document["payload"]
                    if payload["observed_generation"] == args.generation:
                        print(json.dumps(document, indent=2))
                        if payload["health"] != "healthy" or payload["phase"] != args.phase:
                            raise RuntimeError("Observed deployment state differs from the request")
                        return
                except NotFoundError:
                    pass
                await asyncio.sleep(0.2)
    finally:
        await connection.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:14222")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--phase", choices=["running", "stopped"], required=True)
    parser.add_argument("--timeout", type=float, default=30)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
