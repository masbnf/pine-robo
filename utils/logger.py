"""Tiny logging helper so every module logs consistently."""
from __future__ import annotations
import logging
import os


def get_logger(cfg) -> logging.Logger:
    lg = logging.getLogger("smc_bot")
    if lg.handlers:
        return lg
    lg.setLevel(getattr(logging, cfg.LOGGING["level"], logging.INFO))

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    lg.addHandler(ch)

    log_file = cfg.LOGGING.get("log_file")
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    return lg
