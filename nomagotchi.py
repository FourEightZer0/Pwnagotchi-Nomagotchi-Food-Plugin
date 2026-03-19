import json
import logging
import os
import random
import time
from threading import Lock

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK


class Nomagotchi(plugins.Plugin):
    __author__ = "cbirc"
    __version__ = "1.0.0"
    __license__ = "MIT"
    __description__ = "Adds a hunger bar that is fed by captured handshakes."

    DEFAULTS = {
        "max_hunger": 100,
        "start_hunger": 70,
        "handshake_reward": 7,
        "decay_interval_sec": 300,
        "decay_amount": 1,
        "warn_threshold": 25,
        "feed_text": "nom nom nom",
        "feed_text_duration_sec": 6,
        "hungry_text": "im hungry",
        "hungry_texts": ["im hungry", "need handshakes", "feed me pls"],
        "hungry_text_rotation_sec": 8,
        "persist_file": "/root/.pwnagotchi/nomagotchi_state.json",
        "ui_label": "NOMA",
        "ui_position": [0, 93],
        "bar_width": 10,
    }

    def __init__(self):
        self._lock = Lock()
        self._ready = False

        self.max_hunger = self.DEFAULTS["max_hunger"]
        self.hunger = self.DEFAULTS["start_hunger"]
        self.handshake_reward = self.DEFAULTS["handshake_reward"]
        self.decay_interval_sec = self.DEFAULTS["decay_interval_sec"]
        self.decay_amount = self.DEFAULTS["decay_amount"]
        self.warn_threshold = self.DEFAULTS["warn_threshold"]
        self.feed_text = self.DEFAULTS["feed_text"]
        self.feed_text_duration_sec = self.DEFAULTS["feed_text_duration_sec"]
        self.hungry_text = self.DEFAULTS["hungry_text"]
        self.hungry_texts = list(self.DEFAULTS["hungry_texts"])
        self.hungry_text_rotation_sec = self.DEFAULTS["hungry_text_rotation_sec"]
        self.persist_file = self.DEFAULTS["persist_file"]
        self.ui_label = self.DEFAULTS["ui_label"]
        self.ui_position = tuple(self.DEFAULTS["ui_position"])
        self.bar_width = self.DEFAULTS["bar_width"]

        self.feed_count = 0
        self.last_decay_ts = time.time()
        self.feed_text_until = 0.0
        self.current_hungry_text = self.hungry_text
        self.next_hungry_text_ts = 0.0

    @staticmethod
    def _clamp(value, low, high):
        return max(low, min(high, value))

    def _load_options(self):
        opts = self.options or {}
        self.max_hunger = int(opts.get("max_hunger", self.DEFAULTS["max_hunger"]))
        self.hunger = int(opts.get("start_hunger", self.DEFAULTS["start_hunger"]))
        self.handshake_reward = int(opts.get("handshake_reward", self.DEFAULTS["handshake_reward"]))
        self.decay_interval_sec = int(opts.get("decay_interval_sec", self.DEFAULTS["decay_interval_sec"]))
        self.decay_amount = int(opts.get("decay_amount", self.DEFAULTS["decay_amount"]))
        self.warn_threshold = int(opts.get("warn_threshold", self.DEFAULTS["warn_threshold"]))
        self.feed_text = str(opts.get("feed_text", self.DEFAULTS["feed_text"]))
        self.feed_text_duration_sec = int(
            opts.get("feed_text_duration_sec", self.DEFAULTS["feed_text_duration_sec"])
        )
        self.hungry_text = str(opts.get("hungry_text", self.DEFAULTS["hungry_text"]))
        hungry_texts_opt = opts.get("hungry_texts", self.DEFAULTS["hungry_texts"])
        if isinstance(hungry_texts_opt, (list, tuple)):
            self.hungry_texts = [str(v) for v in hungry_texts_opt if str(v).strip()]
        else:
            self.hungry_texts = []
        self.hungry_text_rotation_sec = int(
            opts.get("hungry_text_rotation_sec", self.DEFAULTS["hungry_text_rotation_sec"])
        )
        self.persist_file = opts.get("persist_file", self.DEFAULTS["persist_file"])
        self.ui_label = str(opts.get("ui_label", self.DEFAULTS["ui_label"]))
        self.bar_width = int(opts.get("bar_width", self.DEFAULTS["bar_width"]))

        position = opts.get("ui_position", self.DEFAULTS["ui_position"])
        if isinstance(position, (list, tuple)) and len(position) == 2:
            self.ui_position = (int(position[0]), int(position[1]))

        self.max_hunger = max(1, self.max_hunger)
        self.hunger = self._clamp(self.hunger, 0, self.max_hunger)
        self.handshake_reward = max(1, self.handshake_reward)
        self.decay_interval_sec = max(1, self.decay_interval_sec)
        self.decay_amount = max(1, self.decay_amount)
        self.warn_threshold = self._clamp(self.warn_threshold, 0, self.max_hunger)
        self.feed_text_duration_sec = max(1, self.feed_text_duration_sec)
        self.hungry_text_rotation_sec = max(1, self.hungry_text_rotation_sec)
        self.bar_width = max(3, self.bar_width)

        # Backward compatibility: if hungry_texts is unset, use hungry_text.
        if not self.hungry_texts:
            self.hungry_texts = [self.hungry_text]
        self.current_hungry_text = self.hungry_texts[0]

    def _load_state(self):
        if not os.path.isfile(self.persist_file):
            return

        try:
            with open(self.persist_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            self.hunger = self._clamp(int(state.get("hunger", self.hunger)), 0, self.max_hunger)
            self.feed_count = max(0, int(state.get("feed_count", self.feed_count)))
            self.last_decay_ts = float(state.get("last_decay_ts", self.last_decay_ts))
            logging.info("[nomagotchi] loaded state from %s", self.persist_file)
        except Exception as e:
            logging.warning("[nomagotchi] could not load state: %s", e)

    def _save_state(self):
        state_dir = os.path.dirname(self.persist_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        state = {
            "hunger": self.hunger,
            "feed_count": self.feed_count,
            "last_decay_ts": self.last_decay_ts,
        }

        try:
            with open(self.persist_file, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception as e:
            logging.warning("[nomagotchi] could not save state: %s", e)

    def _bar_text(self):
        pct = int(round((self.hunger / float(self.max_hunger)) * 100))
        return "Food %d%%" % pct

    def _decay_if_needed(self):
        now = time.time()
        elapsed = now - self.last_decay_ts
        if elapsed < self.decay_interval_sec:
            return False

        ticks = int(elapsed // self.decay_interval_sec)
        if ticks <= 0:
            return False

        self.hunger = self._clamp(self.hunger - (ticks * self.decay_amount), 0, self.max_hunger)
        self.last_decay_ts += ticks * self.decay_interval_sec
        return True

    def _feed(self):
        self.hunger = self._clamp(self.hunger + self.handshake_reward, 0, self.max_hunger)
        self.feed_count += 1

    def on_loaded(self):
        self._load_options()
        self._load_state()
        self._ready = True
        logging.info("[nomagotchi] loaded (hunger=%d/%d)", self.hunger, self.max_hunger)

    def on_ready(self, agent):
        logging.info("[nomagotchi] ready")

    def on_ui_setup(self, ui):
        ui.add_element(
            "nomagotchi",
            Text(
                color=BLACK,
                value=self._bar_text(),
                position=self.ui_position,
                font=fonts.Medium,
            ),
        )

    def on_ui_update(self, ui):
        if self._ready:
            with self._lock:
                if self._decay_if_needed():
                    self._save_state()

        now = time.time()
        if now <= self.feed_text_until:
            ui.set("nomagotchi", self.feed_text)
        elif self.hunger <= self.warn_threshold:
            if now >= self.next_hungry_text_ts:
                if len(self.hungry_texts) > 1:
                    candidates = [t for t in self.hungry_texts if t != self.current_hungry_text]
                    if candidates:
                        self.current_hungry_text = random.choice(candidates)
                    else:
                        self.current_hungry_text = random.choice(self.hungry_texts)
                else:
                    self.current_hungry_text = self.hungry_texts[0]

                self.next_hungry_text_ts = now + self.hungry_text_rotation_sec

            ui.set("nomagotchi", self.current_hungry_text)
        else:
            ui.set("nomagotchi", self._bar_text())

    def on_handshake(self, agent, filename, access_point, client_station):
        if not self._ready:
            return

        with self._lock:
            self._feed()
            self.feed_text_until = time.time() + self.feed_text_duration_sec
            self._save_state()
            logging.info(
                "[nomagotchi] fed by handshake (%s). hunger=%d/%d feeds=%d",
                filename,
                self.hunger,
                self.max_hunger,
                self.feed_count,
            )

    def on_epoch(self, agent, epoch, epoch_data):
        if not self._ready:
            return

        changed = False
        with self._lock:
            changed = self._decay_if_needed()
            if changed:
                self._save_state()

        if changed and self.hunger <= self.warn_threshold:
            logging.warning(
                "[nomagotchi] hunger low: %d/%d (feed me with handshakes)",
                self.hunger,
                self.max_hunger,
            )

    def on_unload(self, ui):
        with self._lock:
            self._save_state()

        try:
            ui.remove_element("nomagotchi")
        except Exception:
            pass

        logging.info("[nomagotchi] unloaded")
