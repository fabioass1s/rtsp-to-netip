import struct
import unittest
from datetime import datetime

from netip_bridge import nal, protocol as p
from netip_bridge.client import parse_frames
from netip_bridge.config import ConfigError, parse_config
from netip_bridge.device import ip_hex, resolution, stream_kind
from netip_bridge.onvif import with_credentials
from netip_bridge.source import VideoPacker, mask_url


class ProtocolTest(unittest.TestCase):
    def test_sofia_hash(self):
        # valores conhecidos do firmware Xiongmai
        self.assertEqual(p.sofia_hash(""), "tlJwpbo6")
        self.assertEqual(len(p.sofia_hash("123456")), 8)

    def test_header_roundtrip(self):
        raw = p.pack_json(1001, {"Ret": 100}, session=0x1A, seq=7)
        head, ver, sid, seq, _t, _c, msg_id, length = p.HEADER.unpack(raw[:20])
        self.assertEqual((head, ver, sid, seq, msg_id), (0xFF, 1, 0x1A, 7, 1001))
        self.assertEqual(len(raw), 20 + length)
        self.assertTrue(raw.endswith(b"\n\x00"))
        self.assertEqual(p.parse_json(raw[20:]), {"Ret": 100})

    def test_header_matches_python_dvr_layout(self):
        raw = p.pack(1412, b"abc", session=5, seq=9)
        self.assertEqual(struct.unpack("BB2xII2xHI", raw[:20]), (255, 1, 5, 9, 1412, 3))

    def test_parse_json_binary(self):
        self.assertIsNone(p.parse_json(b"\x00\x00\x01\xfc"))
        self.assertEqual(p.parse_json(b'{"Name": "KeepAlive"}\n\x00'), {"Name": "KeepAlive"})

    def test_session(self):
        self.assertEqual(p.session_hex(10), "0x0000000A")
        self.assertEqual(p.parse_session("0x0000000A"), 10)
        self.assertIsNone(p.parse_session("xyz"))

    def test_timestamp(self):
        ts = p.xm_timestamp(datetime(2024, 5, 17, 13, 45, 30))
        self.assertEqual(((ts >> 26) + 2000, (ts >> 22) & 0xF, (ts >> 17) & 0x1F), (2024, 5, 17))
        self.assertEqual(((ts >> 12) & 0x1F, (ts >> 6) & 0x3F, ts & 0x3F), (13, 45, 30))

    def test_frames_parse_back(self):
        buf = bytearray(
            p.video_iframe(b"IDR", p.CODEC_H264, 25, 1920, 1080)
            + p.video_pframe(b"P1")
            + p.audio_frame(b"\xd5" * 320)
        )
        frames = list(parse_frames(buf))
        self.assertEqual([f[0] for f in frames], [0xFC, 0xFD, 0xFA])
        self.assertEqual(frames[0][1]["width"], 1920)
        self.assertEqual(frames[0][1]["fps"], 25)
        self.assertEqual(frames[0][2], b"IDR")
        self.assertEqual(len(frames[2][2]), 320)
        self.assertEqual(buf, bytearray())


class NalTest(unittest.TestCase):
    SPS = b"\x67\x42\x00\x1f"
    PPS = b"\x68\xce\x3c\x80"
    IDR = b"\x65\x88\x84\x00\x10"
    P = b"\x41\x9a\x02"

    def test_split_annexb(self):
        data = b"\x00\x00\x00\x01" + self.SPS + b"\x00\x00\x01" + self.PPS + b"\x00\x00\x00\x01" + self.IDR
        self.assertEqual(nal.split_annexb(data), [self.SPS, self.PPS, self.IDR])

    def test_avcc_extradata(self):
        avcc = bytes([1, 0x42, 0, 0x1F, 0xFF, 0xE1]) + len(self.SPS).to_bytes(2, "big") + self.SPS
        avcc += bytes([1]) + len(self.PPS).to_bytes(2, "big") + self.PPS
        nals, size = nal.parse_extradata(avcc, nal.H264)
        self.assertEqual((nals, size), ([self.SPS, self.PPS], 4))

    def test_packer_injects_parameter_sets(self):
        extradata = nal.join_annexb([self.SPS, self.PPS])
        packer = VideoPacker(nal.H264, extradata, 25, 1280, 720)
        frame, is_key, payload = packer.pack(nal.join_annexb([self.IDR]), True)
        self.assertTrue(is_key)
        self.assertEqual(nal.split_annexb(payload), [self.SPS, self.PPS, self.IDR])
        self.assertEqual(frame[:4], p.FRAME_I)
        frame, is_key, _ = packer.pack(nal.join_annexb([self.P]), False)
        self.assertFalse(is_key)
        self.assertEqual(frame[:4], p.FRAME_P)

    def test_packer_holds_parameter_only_packets(self):
        packer = VideoPacker(nal.H264, None, 25, 1280, 720)
        self.assertIsNone(packer.pack(nal.join_annexb([self.SPS, self.PPS]), False))
        _, is_key, payload = packer.pack(nal.join_annexb([self.IDR]), False)
        self.assertTrue(is_key)
        self.assertEqual(nal.split_annexb(payload), [self.SPS, self.PPS, self.IDR])


class DeviceTest(unittest.TestCase):
    def test_ip_hex(self):
        self.assertEqual(ip_hex("192.168.1.100"), "0x6401A8C0")

    def test_resolution(self):
        self.assertEqual(resolution(1920, 1080)[1], "1080P")
        self.assertEqual(resolution(1280, 720)[1], "720P")
        self.assertEqual(resolution(704, 576)[1], "D1")
        self.assertEqual(resolution(2560, 1440)[1], "4M")

    def test_stream_kind(self):
        self.assertEqual(stream_kind("Main"), "main")
        self.assertEqual(stream_kind("Extra1"), "sub")
        self.assertEqual(stream_kind(None), "main")


class ConfigTest(unittest.TestCase):
    def test_ports_and_grouping(self):
        cfg = parse_config({
            "cameras": [
                {"name": "A", "rtsp": {"main": "rtsp://a"}},
                {"name": "B", "rtsp": "rtsp://b"},
                {"name": "C", "port": 40000, "rtsp": {"main": "rtsp://c"}},
                {"name": "D", "port": 40000, "onvif": {"host": "10.0.0.4"}},
            ]
        })
        self.assertEqual([d.port for d in cfg.devices], [34567, 34568, 40000])
        self.assertEqual([c.name for c in cfg.devices[2].cameras], ["C", "D"])

    def test_requires_source(self):
        with self.assertRaises(ConfigError):
            parse_config({"cameras": [{"name": "X"}]})


class UrlTest(unittest.TestCase):
    def test_with_credentials(self):
        self.assertEqual(
            with_credentials("rtsp://192.168.0.9:554/Streaming/Channels/101", "admin", "p@ss", host="10.1.1.5"),
            "rtsp://admin:p%40ss@10.1.1.5:554/Streaming/Channels/101",
        )
        self.assertEqual(with_credentials("rtsp://u:p@h/x", "admin", "y"), "rtsp://u:p@h/x")

    def test_mask(self):
        self.assertEqual(mask_url("rtsp://admin:secret@1.2.3.4/x"), "rtsp://admin:***@1.2.3.4/x")


if __name__ == "__main__":
    unittest.main()
