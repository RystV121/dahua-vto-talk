"""Run the simulated VTO as its own process, like a real door on the LAN.

    python -m tests.e2e.run_fake_vto --rtsp 18554 --http 18080 --debug 18081

GET http://127.0.0.1:<debug>/heard reports what the door speaker played.
"""

from __future__ import annotations

import argparse
import asyncio

from aiohttp import web

from tests.fake_vto import FakeVto, dominant_frequency, rms


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp", type=int, default=18554)
    parser.add_argument("--http", type=int, default=18080)
    parser.add_argument("--debug", type=int, default=18081)
    parser.add_argument("--revert-after", type=float, default=None)
    args = parser.parse_args()

    vto = FakeVto(rtsp_port=args.rtsp, http_port=args.http, revert_after=args.revert_after)
    await vto.start()

    async def heard(_request: web.Request) -> web.Response:
        talks = []
        for talk in vto.talks:
            samples = vto.heard_pcm(talk)
            talks.append(
                {
                    "payload_type": talk.payload_type,
                    "packets": len(talk.packets),
                    "packet_sizes": sorted({len(p) for *_x, p in talk.packets}),
                    "samples": len(samples),
                    "seconds": round(len(samples) / 8000, 2),
                    "dominant_hz": dominant_frequency(samples[:16000]) if samples else 0,
                    "rms": round(rms(samples), 1),
                }
            )
        return web.json_response(
            {
                "codec": f"{vto.compression}/{vto.frequency}",
                "set_config_calls": vto.set_config_calls,
                "talk_holder": vto.talk_holder is not None,
                "talks": talks,
                "log": vto.log[-30:],
            }
        )

    app = web.Application()
    app.router.add_get("/heard", heard)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.debug).start()
    print(f"fake VTO ready rtsp={vto.rtsp_port} http={vto.http_port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
