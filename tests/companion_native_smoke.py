"""Opt-in graphical smoke test; run on an unlocked Windows/macOS/Linux desktop."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webview
from companion import Companion


class MemoryStorage:
    position = None

    def set_setting(self, key, value):
        if key == "companion_position":
            self.position = value


storage = MemoryStorage()
window = webview.create_window("ETU-AI - test", html="<html><body>Test du compagnon</body></html>",
                               width=440, height=680, hidden=True)
companion = Companion(window, storage)
window.events.closing += companion.close
window.events.moved += companion.moved
result = []


def exercise_window():
    try:
        companion.toggle()
        time.sleep(1)
        window.move(300, 200)
        time.sleep(1)
        before = (window.x, window.y)
        companion.toggle()
        time.sleep(.5)
        companion.toggle()
        time.sleep(.5)
        after = (window.x, window.y)
        if before != after or storage.position != {"x": before[0], "y": before[1]}:
            raise AssertionError(f"Position non conservée : {before}, {after}, {storage.position}")
        result.append(True)
    finally:
        companion.stopping = True
        window.destroy()


webview.start(exercise_window)
if not result:
    raise SystemExit("Échec du test de fenêtre native")
print("Fenêtre native : ouverture, masquage et position vérifiés")
