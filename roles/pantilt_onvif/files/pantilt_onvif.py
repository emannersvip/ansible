#!/usr/bin/python3
"""Minimal ONVIF PTZ facade for Pi pan-tilt kits.

Frigate only drives PTZ over ONVIF (ContinuousMove + Stop). This process
advertises just enough Device/Media/PTZ SOAP for Frigate 0.14+ and jogs
local servos. Backends:

  * pimoroni   — Pimoroni Pan-Tilt HAT via python3-pantilthat (I2C)
  * sunfounder — Sunfounder PWM pan/tilt (gpiozero AngularServo, BCM 13/12)

Auth is accepted but not enforced; bind to the LAN and keep Frigate as the
only client.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

LOG = logging.getLogger("pantilt-onvif")

LISTEN_HOST = os.environ.get("PANTILT_ONVIF_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PANTILT_ONVIF_PORT", "8000"))
BACKEND = os.environ.get("PANTILT_BACKEND", "pimoroni").strip().lower()
RTSP_PATH = os.environ.get("PANTILT_RTSP_PATH", "rtsp://127.0.0.1:8554/cam")
HOME_PAN = float(os.environ.get("PANTILT_HOME_PAN", "-23"))
HOME_TILT = float(os.environ.get("PANTILT_HOME_TILT", "-13"))
# Degrees per second at |velocity| == 1.0
MAX_RATE = float(os.environ.get("PANTILT_MAX_RATE", "30"))
SAFETY_STOP_S = float(os.environ.get("PANTILT_SAFETY_STOP", "10"))
TICK_S = 0.05
PAN_MIN = float(os.environ.get("PANTILT_PAN_MIN", "-90"))
PAN_MAX = float(os.environ.get("PANTILT_PAN_MAX", "90"))
TILT_MIN = float(os.environ.get("PANTILT_TILT_MIN", "-90"))
TILT_MAX = float(os.environ.get("PANTILT_TILT_MAX", "90"))
PAN_PIN = int(os.environ.get("PANTILT_PAN_PIN", "13"))
TILT_PIN = int(os.environ.get("PANTILT_TILT_PIN", "12"))
SERIAL = os.environ.get("PANTILT_SERIAL", os.uname().nodename)

# Image is HFlip+VFlip in MediaMTX. Positive ONVIF pan = right on the
# flipped image, which matches eom_pantilt.py KEY_RIGHT (pan decreases).
PAN_SIGN = float(os.environ.get("PANTILT_PAN_SIGN", "-1"))
TILT_SIGN = float(os.environ.get("PANTILT_TILT_SIGN", "-1"))

SOAP = "http://www.w3.org/2003/05/soap-envelope"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"

PROFILE_TOKEN = "000"
PTZ_CONFIG_TOKEN = "PTZConfig_000"
PTZ_NODE_TOKEN = "PTZNode_000"
VIDEO_SOURCE_TOKEN = "VideoSource_000"
VIDEO_ENCODER_TOKEN = "VideoEncoder_000"
CONTINUOUS_PT_SPACE = (
    "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
)
PT_SPEED_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


class PimoroniDriver:
    def __init__(self) -> None:
        try:
            import pantilthat
        except ImportError as exc:
            sys.stderr.write(
                "pantilthat is not installed. On Raspberry Pi OS: "
                "sudo apt-get install -y python3-pantilthat python3-smbus\n"
            )
            raise SystemExit(1) from exc
        self._hat = pantilthat
        self._hat.idle_timeout(0.5)

    def read(self) -> tuple[float, float] | None:
        try:
            return float(self._hat.get_pan()), float(self._hat.get_tilt())
        except Exception:
            return None

    def write(self, pan: float, tilt: float) -> None:
        self._hat.pan(pan)
        self._hat.tilt(tilt)


class SunfounderDriver:
    """Sunfounder PWM pan/tilt: BCM 13 pan, BCM 12 tilt (eom_pantilt_sunfounder.py)."""

    def __init__(self) -> None:
        try:
            from gpiozero import AngularServo, Device
            from gpiozero.pins.lgpio import LGPIOFactory
        except ImportError as exc:
            sys.stderr.write(
                "gpiozero is not installed. On Raspberry Pi OS: "
                "sudo apt-get install -y python3-gpiozero python3-lgpio\n"
            )
            raise SystemExit(1) from exc
        # systemd units do not inherit an interactive pin factory. Force
        # lgpio so software PWM works on BCM 12/13 (Pi 4, no hardware PWM).
        if Device.pin_factory is None or type(Device.pin_factory).__name__ != "LGPIOFactory":
            Device.pin_factory = LGPIOFactory()
        # Match servo.py pigpio mapping: 0.5ms..2.5ms over -90..90.
        kw = {"min_pulse_width": 0.0005, "max_pulse_width": 0.0025}
        self._pan = AngularServo(PAN_PIN, min_angle=PAN_MIN, max_angle=PAN_MAX, **kw)
        self._tilt = AngularServo(TILT_PIN, min_angle=TILT_MIN, max_angle=TILT_MAX, **kw)

    def read(self) -> tuple[float, float] | None:
        return None

    def write(self, pan: float, tilt: float) -> None:
        self._pan.angle = pan
        self._tilt.angle = tilt


def _make_driver():
    if BACKEND in ("pimoroni", "pantilthat"):
        return PimoroniDriver()
    if BACKEND in ("sunfounder", "gpiozero", "pigpio"):
        return SunfounderDriver()
    sys.stderr.write(f"Unknown PANTILT_BACKEND={BACKEND!r} (pimoroni|sunfounder)\n")
    raise SystemExit(2)


class PanTiltHat:
    def __init__(self) -> None:
        self.driver = _make_driver()
        pos = self.driver.read()
        if pos is None:
            self.pan = HOME_PAN
            self.tilt = HOME_TILT
        else:
            self.pan, self.tilt = pos
        self.vx = 0.0
        self.vy = 0.0
        self.moving = False
        self.stop_at = 0.0
        self.lock = threading.Lock()
        self._apply(self.pan, self.tilt)
        worker = threading.Thread(target=self._loop, name="pantilt-jog", daemon=True)
        worker.start()

    def _apply(self, pan: float, tilt: float) -> None:
        self.driver.write(pan, tilt)

    def _loop(self) -> None:
        while True:
            time.sleep(TICK_S)
            with self.lock:
                if self.vx == 0.0 and self.vy == 0.0:
                    continue
                if time.time() >= self.stop_at:
                    self.vx = 0.0
                    self.vy = 0.0
                    self.moving = False
                    LOG.info("safety stop at pan=%.1f tilt=%.1f", self.pan, self.tilt)
                    continue
                self.pan = clamp(self.pan + PAN_SIGN * self.vx * MAX_RATE * TICK_S, PAN_MIN, PAN_MAX)
                self.tilt = clamp(self.tilt + TILT_SIGN * self.vy * MAX_RATE * TICK_S, TILT_MIN, TILT_MAX)
                pan, tilt = self.pan, self.tilt
            try:
                self._apply(pan, tilt)
            except Exception:
                LOG.exception("servo write failed")

    def continuous_move(self, pan_v: float, tilt_v: float) -> None:
        if abs(pan_v) < 0.05 and abs(tilt_v) < 0.05:
            self.stop()
            return
        with self.lock:
            self.vx = clamp(pan_v, -1.0, 1.0)
            self.vy = clamp(tilt_v, -1.0, 1.0)
            self.moving = True
            self.stop_at = time.time() + SAFETY_STOP_S
        LOG.info("move vx=%.2f vy=%.2f", pan_v, tilt_v)

    def stop(self) -> None:
        with self.lock:
            self.vx = 0.0
            self.vy = 0.0
            self.moving = False
        LOG.info("stop pan=%.1f tilt=%.1f", self.pan, self.tilt)

    def goto_home(self) -> None:
        with self.lock:
            self.vx = 0.0
            self.vy = 0.0
            self.moving = False
            self.pan = HOME_PAN
            self.tilt = HOME_TILT
        self._apply(HOME_PAN, HOME_TILT)
        LOG.info("home pan=%.1f tilt=%.1f", HOME_PAN, HOME_TILT)

    def status(self) -> tuple[float, float, str]:
        with self.lock:
            return self.pan, self.tilt, "MOVING" if self.moving else "IDLE"


HAT = PanTiltHat()


def _envelope(inner: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{SOAP}" xmlns:tds="{TDS}" xmlns:trt="{TRT}" '
        f'xmlns:tptz="{TPTZ}" xmlns:tt="{TT}">'
        "<s:Header/><s:Body>"
        f"{inner}"
        "</s:Body></s:Envelope>"
    )
    return xml.encode("utf-8")


def _fault(reason: str, detail: str = "") -> bytes:
    inner = (
        "<s:Fault>"
        "<s:Code><s:Value>s:Receiver</s:Value></s:Code>"
        f"<s:Reason><s:Text xml:lang='en'>{reason}</s:Text></s:Reason>"
        f"<s:Detail>{detail}</s:Detail>"
        "</s:Fault>"
    )
    return _envelope(inner)


def utc_now_parts() -> datetime:
    return datetime.now(timezone.utc)


def get_system_date_and_time() -> bytes:
    now = utc_now_parts()
    return _envelope(
        "<tds:GetSystemDateAndTimeResponse>"
        "<tds:SystemDateAndTime>"
        "<tt:DateTimeType>NTP</tt:DateTimeType>"
        "<tt:DaylightSavings>false</tt:DaylightSavings>"
        "<tt:UTCDateTime>"
        f"<tt:Time><tt:Hour>{now.hour}</tt:Hour>"
        f"<tt:Minute>{now.minute}</tt:Minute>"
        f"<tt:Second>{now.second}</tt:Second></tt:Time>"
        f"<tt:Date><tt:Year>{now.year}</tt:Year>"
        f"<tt:Month>{now.month}</tt:Month>"
        f"<tt:Day>{now.day}</tt:Day></tt:Date>"
        "</tt:UTCDateTime>"
        "</tds:SystemDateAndTime>"
        "</tds:GetSystemDateAndTimeResponse>"
    )


def get_device_information() -> bytes:
    if BACKEND in ("sunfounder", "gpiozero", "pigpio"):
        manufacturer, model, hw = "Sunfounder", "Pan-Tilt", "PWM-13-12"
    else:
        manufacturer, model, hw = "Pimoroni", "Pan-Tilt HAT", "PIM213"
    return _envelope(
        "<tds:GetDeviceInformationResponse>"
        f"<tds:Manufacturer>{manufacturer}</tds:Manufacturer>"
        f"<tds:Model>{model}</tds:Model>"
        "<tds:FirmwareVersion>0.2.0</tds:FirmwareVersion>"
        f"<tds:SerialNumber>{SERIAL}</tds:SerialNumber>"
        f"<tds:HardwareId>{hw}</tds:HardwareId>"
        "</tds:GetDeviceInformationResponse>"
    )


def get_capabilities(base: str) -> bytes:
    return _envelope(
        "<tds:GetCapabilitiesResponse><tds:Capabilities>"
        f"<tt:Device><tt:XAddr>{base}/onvif/device_service</tt:XAddr></tt:Device>"
        f"<tt:Media><tt:XAddr>{base}/onvif/media_service</tt:XAddr></tt:Media>"
        f"<tt:PTZ><tt:XAddr>{base}/onvif/ptz_service</tt:XAddr></tt:PTZ>"
        "</tds:Capabilities></tds:GetCapabilitiesResponse>"
    )


def get_services(base: str) -> bytes:
    services = [
        ("http://www.onvif.org/ver10/device/wsdl", f"{base}/onvif/device_service"),
        ("http://www.onvif.org/ver10/media/wsdl", f"{base}/onvif/media_service"),
        ("http://www.onvif.org/ver20/ptz/wsdl", f"{base}/onvif/ptz_service"),
    ]
    parts = ["<tds:GetServicesResponse>"]
    for ns, xaddr in services:
        parts.append(
            "<tds:Service>"
            f"<tds:Namespace>{ns}</tds:Namespace>"
            f"<tds:XAddr>{xaddr}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>"
            "</tds:Service>"
        )
    parts.append("</tds:GetServicesResponse>")
    return _envelope("".join(parts))


def _ptz_config() -> str:
    return (
        f'<tt:PTZConfiguration token="{PTZ_CONFIG_TOKEN}">'
        "<tt:Name>PTZConfig</tt:Name>"
        "<tt:UseCount>1</tt:UseCount>"
        f"<tt:NodeToken>{PTZ_NODE_TOKEN}</tt:NodeToken>"
        f"<tt:DefaultContinuousPanTiltVelocitySpace>{CONTINUOUS_PT_SPACE}</tt:DefaultContinuousPanTiltVelocitySpace>"
        "<tt:DefaultPTZSpeed>"
        f'<tt:PanTilt space="{PT_SPEED_SPACE}" x="0.5" y="0.5"/>'
        "</tt:DefaultPTZSpeed>"
        "<tt:DefaultPTZTimeout>PT10S</tt:DefaultPTZTimeout>"
        "</tt:PTZConfiguration>"
    )


def get_profiles() -> bytes:
    return _envelope(
        "<trt:GetProfilesResponse>"
        f'<trt:Profiles token="{PROFILE_TOKEN}" fixed="true">'
        "<tt:Name>MainStream</tt:Name>"
        f'<tt:VideoSourceConfiguration token="{VIDEO_SOURCE_TOKEN}">'
        "<tt:Name>VideoSource</tt:Name><tt:UseCount>1</tt:UseCount>"
        f"<tt:SourceToken>{VIDEO_SOURCE_TOKEN}</tt:SourceToken>"
        '<tt:Bounds x="0" y="0" width="1920" height="1080"/>'
        "</tt:VideoSourceConfiguration>"
        f'<tt:VideoEncoderConfiguration token="{VIDEO_ENCODER_TOKEN}">'
        "<tt:Name>VideoEncoder</tt:Name><tt:UseCount>1</tt:UseCount>"
        "<tt:Encoding>H264</tt:Encoding>"
        "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
        "</tt:VideoEncoderConfiguration>"
        f"{_ptz_config()}"
        "</trt:Profiles>"
        "</trt:GetProfilesResponse>"
    )


def get_stream_uri() -> bytes:
    return _envelope(
        "<trt:GetStreamUriResponse>"
        "<trt:MediaUri>"
        f"<tt:Uri>{RTSP_PATH}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
        "<tt:Timeout>PT60S</tt:Timeout>"
        "</trt:MediaUri>"
        "</trt:GetStreamUriResponse>"
    )


def get_video_sources() -> bytes:
    return _envelope(
        "<trt:GetVideoSourcesResponse>"
        f'<trt:VideoSources token="{VIDEO_SOURCE_TOKEN}">'
        "<tt:Framerate>15</tt:Framerate>"
        "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
        "</trt:VideoSources>"
        "</trt:GetVideoSourcesResponse>"
    )


def get_nodes() -> bytes:
    return _envelope(
        "<tptz:GetNodesResponse>"
        f'<tptz:PTZNode token="{PTZ_NODE_TOKEN}">'
        "<tt:Name>PTZ Node</tt:Name>"
        "<tt:SupportedPTZSpaces>"
        "<tt:ContinuousPanTiltVelocitySpace>"
        f"<tt:URI>{CONTINUOUS_PT_SPACE}</tt:URI>"
        "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
        "</tt:ContinuousPanTiltVelocitySpace>"
        "<tt:PanTiltSpeedSpace>"
        f"<tt:URI>{PT_SPEED_SPACE}</tt:URI>"
        "<tt:XRange><tt:Min>0.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
        "</tt:PanTiltSpeedSpace>"
        "</tt:SupportedPTZSpaces>"
        "<tt:MaximumNumberOfPresets>1</tt:MaximumNumberOfPresets>"
        "<tt:HomeSupported>true</tt:HomeSupported>"
        "</tptz:PTZNode>"
        "</tptz:GetNodesResponse>"
    )


def get_configuration_options() -> bytes:
    return _envelope(
        "<tptz:GetConfigurationOptionsResponse>"
        "<tptz:PTZConfigurationOptions>"
        "<tt:Spaces>"
        "<tt:ContinuousPanTiltVelocitySpace>"
        f"<tt:URI>{CONTINUOUS_PT_SPACE}</tt:URI>"
        "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
        "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
        "</tt:ContinuousPanTiltVelocitySpace>"
        "<tt:PanTiltSpeedSpace>"
        f"<tt:URI>{PT_SPEED_SPACE}</tt:URI>"
        "<tt:XRange><tt:Min>0.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
        "</tt:PanTiltSpeedSpace>"
        "</tt:Spaces>"
        "<tt:PTZTimeout><tt:Min>PT1S</tt:Min><tt:Max>PT60S</tt:Max></tt:PTZTimeout>"
        "</tptz:PTZConfigurationOptions>"
        "</tptz:GetConfigurationOptionsResponse>"
    )


def get_service_capabilities() -> bytes:
    return _envelope(
        "<tptz:GetServiceCapabilitiesResponse>"
        '<tptz:Capabilities EFlip="false" Reverse="false" '
        'GetCompatibleConfigurations="false" MoveStatus="true" StatusPosition="true"/>'
        "</tptz:GetServiceCapabilitiesResponse>"
    )


def get_presets() -> bytes:
    return _envelope(
        "<tptz:GetPresetsResponse>"
        '<tptz:Preset token="home"><tt:Name>home</tt:Name></tptz:Preset>'
        "</tptz:GetPresetsResponse>"
    )


def get_status() -> bytes:
    pan, tilt, move = HAT.status()
    # Normalize angles into ONVIF [-1, 1]
    nx = (pan - PAN_MIN) / (PAN_MAX - PAN_MIN) * 2.0 - 1.0
    ny = (tilt - TILT_MIN) / (TILT_MAX - TILT_MIN) * 2.0 - 1.0
    now = utc_now_parts().strftime("%Y-%m-%dT%H:%M:%SZ")
    return _envelope(
        "<tptz:GetStatusResponse><tptz:PTZStatus>"
        "<tt:Position>"
        f'<tt:PanTilt x="{nx:.4f}" y="{ny:.4f}"/>'
        "</tt:Position>"
        "<tt:MoveStatus>"
        f"<tt:PanTilt>{move}</tt:PanTilt>"
        "<tt:Zoom>IDLE</tt:Zoom>"
        "</tt:MoveStatus>"
        f"<tt:UtcTime>{now}</tt:UtcTime>"
        "</tptz:PTZStatus></tptz:GetStatusResponse>"
    )


def simple(ns: str, op: str) -> bytes:
    return _envelope(f"<{ns}:{op}Response/>")


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def extract_action(root: ET.Element) -> tuple[str, ET.Element | None]:
    body = None
    for child in root:
        if localname(child.tag) == "Body":
            body = child
            break
    if body is None or len(body) == 0:
        raise ValueError("SOAP Body missing")
    op = body[0]
    return localname(op.tag), op


def find_pantilt(elem: ET.Element) -> tuple[float, float]:
    for node in elem.iter():
        if localname(node.tag) == "PanTilt":
            return float(node.get("x") or 0), float(node.get("y") or 0)
    return 0.0, 0.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - " + fmt, self.address_string(), *args)

    def _send(self, body: bytes, status: int = 200, content_type: str = "application/soap+xml") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/"):
            pan, tilt, move = HAT.status()
            payload = (
                f'{{"ok":true,"pan":{pan:.1f},"tilt":{tilt:.1f},"move":"{move}"}}\n'
            ).encode()
            self._send(payload, content_type="application/json")
            return
        self._send(b"not found\n", status=404, content_type="text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        host = self.headers.get("Host") or f"{LISTEN_HOST}:{LISTEN_PORT}"
        base = f"http://{host}"
        try:
            root = ET.fromstring(raw)
            action, op = extract_action(root)
            LOG.debug("ONVIF %s %s", self.path, action)
            if action == "GetCapabilities":
                body = get_capabilities(base)
            elif action == "GetServices":
                body = get_services(base)
            elif action == "GetSystemDateAndTime":
                body = get_system_date_and_time()
            elif action == "GetDeviceInformation":
                body = get_device_information()
            elif action == "GetProfiles":
                body = get_profiles()
            elif action == "GetProfile":
                body = get_profiles()
            elif action == "GetVideoSources":
                body = get_video_sources()
            elif action == "GetStreamUri":
                body = get_stream_uri()
            elif action == "GetConfigurationOptions":
                body = get_configuration_options()
            elif action == "GetServiceCapabilities":
                body = get_service_capabilities()
            elif action == "GetNodes":
                body = get_nodes()
            elif action == "GetNode":
                body = get_nodes()
            elif action == "GetPresets":
                body = get_presets()
            elif action == "GetStatus":
                body = get_status()
            elif action == "ContinuousMove":
                if op is None:
                    raise ValueError("ContinuousMove body missing")
                pan_v, tilt_v = find_pantilt(op)
                HAT.continuous_move(pan_v, tilt_v)
                body = simple("tptz", "ContinuousMove")
            elif action == "Stop":
                HAT.stop()
                body = simple("tptz", "Stop")
            elif action in ("GotoHomePosition", "GotoPreset"):
                HAT.goto_home()
                body = simple("tptz", action)
            elif action in (
                "GetWsdlUrl",
                "GetHostname",
                "GetNetworkInterfaces",
                "GetScopes",
                "GetDNS",
                "GetNTP",
                "GetNetworkProtocols",
                "GetDeviceInformation",
            ):
                body = get_device_information() if action == "GetDeviceInformation" else simple("tds", action)
            else:
                LOG.warning("unsupported ONVIF action %s", action)
                body = _fault("Action not supported", action)
            self._send(body)
        except Exception as exc:
            LOG.exception("ONVIF request failed")
            self._send(_fault("Device error", str(exc)), status=500)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PANTILT_ONVIF_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    LOG.info(
        "ONVIF PTZ listening on %s:%s backend=%s rtsp=%s home=%.1f,%.1f",
        LISTEN_HOST,
        LISTEN_PORT,
        BACKEND,
        RTSP_PATH,
        HOME_PAN,
        HOME_TILT,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutdown")
        httpd.server_close()


if __name__ == "__main__":
    main()
