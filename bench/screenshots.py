"""Capture product screenshots from the running stack (Playwright/Chromium)."""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("API", "http://localhost:8000")
OUT = "docs/img"


def main():
    degr = open("/tmp/degvins").read().split()
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1.5)
        pg.goto(BASE + "/")
        pg.wait_for_selector("#risk tr")
        # trigger live degradation *after* the page subscribed to SSE
        sim = subprocess.Popen([sys.executable, "-m", "fleetpulse.simulator.run", "--rate", "400", "--seconds", "40",
                                "--degrade", *degr, "--degrade-rate", "0.08"],
                               env={**os.environ, "BUS": "redis", "PARTITIONS": "8"}, stdout=subprocess.DEVNULL)
        pg.wait_for_selector("#feed div.new", timeout=60_000)
        time.sleep(8)
        pg.fill("#hours", "24")
        pg.click("#plan")
        pg.wait_for_selector("#planout table")
        pg.fill("#q", "which vehicles are most likely to break down? top 5")
        pg.press("#q", "Enter")
        pg.wait_for_selector("#chatlog .q")
        time.sleep(1.5)
        pg.screenshot(path=f"{OUT}/dashboard.png", full_page=True)
        pg.click("#risk tr.row")
        pg.wait_for_selector("#drawer.open h3")
        time.sleep(1)
        pg.screenshot(path=f"{OUT}/vehicle_detail.png")
        pg.click("#wo")
        time.sleep(1)
        pg.screenshot(path=f"{OUT}/work_order.png")
        sim.wait()
        ops = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5)
        ops.goto(BASE + "/ops")
        time.sleep(40)
        ops.screenshot(path=f"{OUT}/ops_dashboard.png", full_page=True)
        b.close()


if __name__ == "__main__":
    main()
