"""Conservative catalogue: hardware identifiers are evidence, not proof of function.

USB IDs often describe the USB bridge rather than the radio chipset. No RX/TX
capability is inferred from a generic USB/serial bridge or spidev endpoint.
"""
from __future__ import annotations

import re

# Known, relatively unambiguous USB radio interfaces. Different revisions may
# have different firmware, antenna chains, and supported frequency ranges.
USB_RADIOS = {
    ("0bda", "2832"): ("RTL2832U-family SDR", "sdr", "receive", "rtl2832u"),
    ("0bda", "2838"): ("RTL2832U-family SDR", "sdr", "receive", "rtl2832u"),
    ("1d50", "6089"): ("HackRF-family SDR", "sdr", "transceiver", "hackrf"),
    ("1d50", "60a1"): ("Airspy-family SDR", "sdr", "receive", "airspy"),
    ("1d50", "60a2"): ("Airspy-family SDR", "sdr", "receive", "airspy"),
    ("2cf0", "5246"): ("bladeRF-family SDR", "sdr", "transceiver", "bladerf"),
    ("0456", "b673"): ("ADALM-Pluto SDR", "sdr", "transceiver", "ad936x"),
}

RADIO_CHIPS = {
    "sx1272": ("lora", "transceiver"), "sx1276": ("lora", "transceiver"),
    "sx1278": ("lora", "transceiver"), "sx1261": ("lora", "transceiver"),
    "sx1262": ("lora", "transceiver"), "sx1280": ("lora", "transceiver"),
    "sx1281": ("lora", "transceiver"), "lr1110": ("lora", "transceiver"),
    "lr1121": ("lora", "transceiver"), "sx1301": ("lora-concentrator", "transceiver"),
    "sx1302": ("lora-concentrator", "transceiver"),
    "sx1303": ("lora-concentrator", "transceiver"),
    "sx1250": ("lora", "transceiver"),
    "si4703": ("fm", "receive"), "si4702": ("fm", "receive"),
    "si470x": ("fm", "receive"), "si4735": ("broadcast-radio", "receive"),
    "si4730": ("broadcast-radio", "receive"),
    "tea5767": ("fm", "receive"), "rda5807": ("fm", "receive"),
    "rda5807m": ("fm", "receive"), "qn8035": ("fm", "receive"),
    "bk1080": ("fm", "receive"), "si4825": ("broadcast-radio", "receive"),
    "cc1101": ("ism", "transceiver"), "cc2500": ("ism", "transceiver"),
    "nrf24l01": ("ism", "transceiver"), "rfm69": ("ism", "transceiver"),
    "si4432": ("ism", "transceiver"), "at86rf233": ("802.15.4", "transceiver"),
    "at86rf215": ("802.15.4", "transceiver"),
}


def chip_from_signature(text: str):
    for term, value in RADIO_CHIPS.items():
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text.lower()):
            return term, value[0], value[1]
    return None


def capability(mode: str):
    return {"receive": (True, False), "transceiver": (True, True),
            "transmit": (False, True)}.get(mode, (None, None))
