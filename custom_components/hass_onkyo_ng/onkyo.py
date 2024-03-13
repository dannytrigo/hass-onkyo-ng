from __future__ import annotations
from collections import defaultdict
from eiscp.core import Receiver, command_to_iscp, iscp_to_command
from .const import *
from .util import dict_merge
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
import threading
import logging
from typing import Any
import xml.etree.ElementTree as ET

_LOGGER = logging.getLogger(__name__)
_ZONE_NAMES = ("main", "zone2", "zone3", "zone4")

class OnkyoReceiver:
    """Class to manage fetching Onkyo data from the receiver."""

    def __init__(
        self,
        host: str,
        hass: HomeAssistant,
        max_volume=ONKYO_SUPPORTED_MAX_VOLUME,
        receiver_max_volume=ONKYO_DEFAULT_RECEIVER_MAX_VOLUME,
    ) -> None:
        """Initialize."""
        self._host = host
        self._receiver = Receiver(host)
        self._receiver.on_message = lambda msg: self._on_message_async(msg)
        self._reverse_source_mapping = {}
        self._reverse_sound_mode_mapping = {}
        self._receiver_info = None
        self._max_volume = max_volume
        self._receiver_max_volume = receiver_max_volume
        self._hdmi_out_supported = True
        self._audio_info_supported = True
        self._video_info_supported = True
        self.listeners = []
        self._sync_pending: threading.Event = None
        self._sync_command_prefix: str = None
        self._sync_result = None
        if hass:
            self._storage = Store[dict[str, Any]](hass, 1, f'onkyo_{host}')
        else:
            self._storage = None

        self.data = {
            ATTR_SOURCES: [],
            ATTR_SOUND_MODES: [],
            ATTR_PRESET: None,
            ATTR_HDMI_OUT: None,
            ATTR_RECEIVER_INFORMATION: {},
        }
        for zone in _ZONE_NAMES:
            key = f"{ATTR_ZONE}_{zone}"
            self.data[key] = {
                ATTR_POWER: None,
                ATTR_AUDIO_INFO: None,
                ATTR_VIDEO_INFO: None,
                ATTR_MUTE: None,
                ATTR_VOLUME: None,
                ATTR_SOURCE: None,
                ATTR_SOUND_MODE: None,
            }

        # Perform synchronously to ensure we have basic data
        self.command_sync('dock.receiver-information=query')

    @property
    def zones(self):
        return self._receiver_info['zones']

    async def load_data(self):
        if self._storage:
            data = await self._storage.async_load()
            _LOGGER.info(f"Loaded data {data}")
            if data:
                main_zone = data.get('zone_main', {})
                self._reverse_source_mapping = main_zone.get('reverse_source_mapping', {})
                self._reverse_sound_mode_mapping = main_zone.get('reverse_sound_mode_mapping', {})
                self.data[ATTR_SOURCES] = list(self._reverse_source_mapping.keys())
                self.data[ATTR_SOUND_MODES] = list(self._reverse_sound_mode_mapping.keys())
                #self._receiver_info = data.get('receiver_information', {})
                #self.data[ATTR_RECEIVER_INFORMATION] = self._receiver_info

                for listener in self.listeners:
                    listener(self.data)

    def store_data(self):
        if self._storage:
            self._storage.async_delay_save(self._data_to_save, 1)

    def _data_to_save(self):
        data = {
            'zone_main': {
                'reverse_source_mapping': self._reverse_source_mapping,
                'reverse_sound_mode_mapping': self._reverse_sound_mode_mapping,
            },
        }
        return data

    def disconnect(self):
        _LOGGER.info("Disconnect from receiver")
        self._receiver.disconnect()

    def register_listener(self, listener):
        self.listeners.append(listener)

    def _on_message_async(self, message):
        """Received a message from the receiver"""
        updates = defaultdict(dict)
        try:
            message_decoded = iscp_to_command(message, with_zone=True)
            _LOGGER.info(f"Received command: {message_decoded}")
            zone, command, attrib = message_decoded
            if zone in _ZONE_NAMES:
                zone_key = f"{ATTR_ZONE}_{zone}"
                if command in ["system-power", "power"]:
                    updates[zone_key][ATTR_POWER] = POWER_ON if attrib == "on" else POWER_OFF
                elif command == "audio-information":
                    info = self._parse_audio_information((command, attrib))
                    updates[zone_key][ATTR_AUDIO_INFO] = info
                elif command == "video-information":
                    info = self._parse_video_information((command, attrib))
                    updates[zone_key][ATTR_VIDEO_INFO] = info
                elif command in ["audio-muting", "muting"]:
                    updates[zone_key][ATTR_MUTE] = attrib == "on"
                elif command in ("master-volume", "volume"):
                    updates[zone_key][ATTR_VOLUME] = attrib / (self._receiver_max_volume * self._max_volume / 100)
                elif command in ["input-selector", "selector"]:
                    sources = self._parse_onkyo_payload((command, attrib))
                    source = "_".join(sources)
                    if not source in self._reverse_source_mapping:
                        # New source found
                        self._reverse_source_mapping[source] = sources[0]
                        updates[ATTR_SOURCES] = list(self._reverse_source_mapping.keys())
                        self.store_data()
                    updates[zone_key][ATTR_SOURCE] = source
                elif command == "preset":
                    updates[zone_key][ATTR_PRESET] = attrib
                elif command == "hdmi-output-selector":
                    updates[zone_key][ATTR_HDMI_OUT] = ",".join(attrib)
                    if attrib == "N/A":
                        self._hdmi_out_supported = False
                elif command == "listening-mode":
                    sound_modes = self._parse_onkyo_payload((command, attrib))
                    sound_mode = "_".join(sound_modes)
                    if not sound_mode in self._reverse_sound_mode_mapping:
                        self._reverse_sound_mode_mapping[sound_mode] = sound_modes[0]
                        updates[ATTR_SOUND_MODES] = list(self._reverse_sound_mode_mapping.keys())
                        self.store_data()
                    updates[zone_key][ATTR_SOUND_MODE] = sound_mode
            elif zone == 'dock':
                if command == "receiver-information":
                    _LOGGER.info("Got receiver info. Parsing")
                    info = self._parse_receiver_information(attrib)
                    self._receiver_info = info
                    updates[ATTR_RECEIVER_INFORMATION] = info
                    self.store_data()
            else:
                _LOGGER.info(f"Ignoring zone {zone}")
        except ValueError:
            _LOGGER.debug(f"Cannot decode raw message: {message}")
        if updates:
            dict_merge(self.data, updates)
            _LOGGER.debug(f"Dispatch data to {len(self.listeners)} listeners")
            for listener in self.listeners:
                listener(self.data)
        if self._sync_pending:
            _LOGGER.info(f"Received {message} whilst waiting for sync response {self._sync_command_prefix}")
            if self._sync_command_prefix == message[:3]:
                _LOGGER.info("Handled sync response")
                self._sync_result = message
                self._sync_pending.set()
                return

    def _parse_onkyo_payload(self, payload):
        """Parse a payload returned from the eiscp library."""
        if isinstance(payload, bool):
            # command not supported by the device
            return False

        if len(payload) < 2:
            # no value
            return None

        if isinstance(payload[1], str):
            return payload[1].split(",")

        return payload[1]

    def _tuple_get(self, tup, index, default=None):
        """Return a tuple item at index or a default value if it doesn't exist."""
        return (tup[index : index + 1] or [default])[0]

    def _parse_audio_information(self, audio_information_raw):
        values = self._parse_onkyo_payload(audio_information_raw)
        if values is False or values is None:
            self._audio_info_supported = False
            return None

        info = {
            "format": self._tuple_get(values, 1),
            "input_frequency": self._tuple_get(values, 2),
            "input_channels": self._tuple_get(values, 3),
            "listening_mode": self._tuple_get(values, 4),
            "output_channels": self._tuple_get(values, 5),
            "output_frequency": self._tuple_get(values, 6),
        }
        return info

    def _parse_video_information(self, video_information_raw):
        values = self._parse_onkyo_payload(video_information_raw)
        if values is False or values is None:
            self._video_info_supported = False
            return None

        info = {
            "input_resolution": self._tuple_get(values, 1),
            "input_color_schema": self._tuple_get(values, 2),
            "input_color_depth": self._tuple_get(values, 3),
            "output_resolution": self._tuple_get(values, 5),
            "output_color_schema": self._tuple_get(values, 6),
            "output_color_depth": self._tuple_get(values, 7),
            "picture_mode": self._tuple_get(values, 8),
        }
        return info

    def _parse_receiver_information(self, receiver_information_xml) -> dict[str, Any]:
        data = ET.fromstring(receiver_information_xml)
        device = data.find('device')
        model = device.find('model').text
        productid = device.find('productid').text
        serial = device.find('deviceserial').text
        macaddress = device.find('macaddress').text
        zones = {}
        for zone in device.find('zonelist').findall('zone'):
            if int(zone.attrib['value']) > 0:
                zones[zone.attrib['id']] = {
                    'name': zone.attrib['name'].lower(),
                    'volmax': zone.attrib['volmax'],
                }

        sources = {}
        for source in device.find('selectorlist').findall('selector'):
            if int(source.attrib['value']) > 0:
                # Assume this is a bitwise identifier for which zones support this source
                source_zones = int(source.attrib['zone'], 16)
                zone_ids = []
                for zone in zones.keys():
                    zone_id = int(zone, 16)
                    if source_zones & (1 << (zone_id - 1)):
                        zone_ids.append(zone)
                sources[source.attrib['id']] = {
                    'name': source.attrib['name'],
                    'zones': zone_ids,
                }
        data = {
            'model': model,
            'productid': productid,
            'serial': serial,
            'macaddress': macaddress,
            'zones': zones,
            'sources': sources,
        }
        _LOGGER.info(f"Parsed {data}")
        return data

    def raw(self, command):
        """Send a raw command."""
        _LOGGER.debug(f"Sending raw command: {command}")
        self._receiver._ensure_socket_connected()
        self._receiver.send(command)

    def command(self, command):
        """Send an eiscp command."""
        _LOGGER.debug(f"Sending command: {command}")
        self._receiver._ensure_socket_connected()
        self._receiver.send(command_to_iscp(command))

    def raw_sync(self, raw_command: str):
        """Run a raw eiscp command synchronously."""
        _LOGGER.info(f"Sending sync command {raw_command}")
        self._sync_command_prefix = raw_command[:3]
        self._sync_pending = threading.Event()
        self._receiver.send(raw_command)
        try:
            if not self._sync_pending.wait(10):
                raise ValueError("Timeout waiting for response")
            result_raw = self._sync_result
            _LOGGER.info(f"Result: {result_raw}")
            return result_raw
        finally:
            self._sync_pending = None
            self._sync_command_prefix = None
            self._sync_result = None

    def command_sync(self, command: str):
        """Run an eiscp command synchronously."""
        _LOGGER.info(f"Sending sync command {command}")
        raw_command = command_to_iscp(command)
        result_raw = self.raw_sync(raw_command)
        result = iscp_to_command(result_raw)
        _LOGGER.info(f"Result: {result}")
        return result

    def update(self):
        """Get the latest state from the device."""
        # some basic info
        self.data[ATTR_NAME] = self._receiver_info["model"]
        self.data[ATTR_IDENTIFIER] = self._receiver_info["macaddress"]

        for zone in _ZONE_NAMES:
            # retrieve power information
            self.command(f"{zone}.power=query")
            # retrieve volume information
            self.command(f"{zone}.volume=query")

            if zone == "main":
                # retrieve audio information
                self.command("main.audio-information=query")
                # retrieve video information
                self.command("main.video-information=query")
                # retrieve sound mode information
                self.command("main.listening-mode=query")
                # retrieve preset information
                self.command("main.preset=query")
                # If the following command is sent to a device with only one HDMI out,
                # the display shows 'Not Available'.
                # We avoid this by checking if HDMI out is supported
                if self._hdmi_out_supported:
                    self.command("main.hdmi-output-selector=query")
                # retrieve mute information
                self.command("main.audio-muting=query")
                # retrieve source information
                self.command(f"{zone}.input-selector=query")

            else:
                # retrieve mute information
                self.command(f"{zone}.muting=query")
                # retrieve source information
                self.command(f"{zone}.selector=query")
