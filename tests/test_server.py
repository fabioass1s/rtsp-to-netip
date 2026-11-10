import asyncio
import unittest
from unittest import mock

from netip_bridge import protocol as p
from netip_bridge import server as server_mod
from netip_bridge.config import NetIPConfig
from netip_bridge.server import NetIPServer
from netip_bridge.source import Subscriber

PARAMS = {"Channel": 0, "CombinMode": "NONE", "StreamType": "Main", "TransMode": "TCP"}


class FakeSource:
    name = "fake/main"

    def __init__(self):
        self.subs = []

    def subscribe(self):
        sub = Subscriber(10)
        self.subs.append(sub)
        return sub

    def unsubscribe(self, sub):
        if sub in self.subs:
            self.subs.remove(sub)


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.src = FakeSource()

    async def prepare(self):
        pass

    def source(self, kind):
        return self.src


class FakeDevice:
    port = 0
    name = "fake"

    def __init__(self):
        self.channels = [FakeChannel()]

    def channel(self, index):
        return self.channels[index] if index == 0 else None

    def login_info(self, session_hex):
        return {"AliveInterval": 20, "ChannelNum": 1, "Ret": 100, "SessionID": session_hex}


async def send(writer, msg_id, body):
    writer.write(p.pack_json(msg_id, body, version=0))
    await writer.drain()


async def recv(reader):
    header = await asyncio.wait_for(reader.readexactly(p.HEADER_SIZE), 2)
    msg_id, length = p.HEADER.unpack(header)[6:8]
    payload = await reader.readexactly(length)
    return msg_id, payload


def monitor(action, sid):
    return {"Name": "OPMonitor", "SessionID": sid, "OPMonitor": {"Action": action, "Parameter": PARAMS}}


class MonitorFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = FakeDevice()
        self.server = NetIPServer(self.device, NetIPConfig(bind="127.0.0.1", password="x"))
        await self.server.start()
        self.port = self.server._server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
        await self.server.stop()

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        return reader, writer

    async def login(self, reader, writer):
        await send(writer, p.LOGIN_REQ, {"UserName": "admin", "PassWord": p.sofia_hash("x")})
        msg_id, payload = await recv(reader)
        self.assertEqual(msg_id, p.LOGIN_RSP)
        return p.parse_json(payload)["SessionID"]

    def push_frame(self):
        sub = self.device.channels[0].src.subs[0]
        sub.offer(p.video_iframe(b"\x00\x00\x00\x01\x65", p.CODEC_H264, 25, 640, 480), True)

    async def test_separate_media_connection_like_dvr(self):
        with mock.patch.object(server_mod, "IDLE_TIMEOUT", 0.3):
            cmd_r, cmd_w = await self.connect()
            sid = await self.login(cmd_r, cmd_w)
            med_r, med_w = await self.connect()
            await send(med_w, p.MONITOR_CLAIM, monitor("Claim", sid))
            self.assertEqual((await recv(med_r))[0], p.MONITOR_CLAIM + 1)
            await send(cmd_w, p.MONITOR_REQ, monitor("Start", sid))
            # o DVR espera a resposta do Start na conexão de comando
            msg_id, payload = await recv(cmd_r)
            self.assertEqual((msg_id, p.parse_json(payload)["Ret"]), (p.MONITOR_REQ + 1, p.RET_OK))
            # a conexão de mídia não recebe keepalive e não pode cair por inatividade
            await asyncio.sleep(0.8)
            self.push_frame()
            self.assertEqual((await recv(med_r))[0], p.MONITOR_DATA)

    async def test_same_connection_like_python_dvr(self):
        reader, writer = await self.connect()
        sid = await self.login(reader, writer)
        await send(writer, p.MONITOR_CLAIM, monitor("Claim", sid))
        self.assertEqual((await recv(reader))[0], p.MONITOR_CLAIM + 1)
        await send(writer, p.MONITOR_REQ, monitor("Start", sid))
        await asyncio.sleep(0.1)
        self.push_frame()
        # sem resposta JSON ao Start: o próximo pacote já é vídeo
        self.assertEqual((await recv(reader))[0], p.MONITOR_DATA)


if __name__ == "__main__":
    unittest.main()
