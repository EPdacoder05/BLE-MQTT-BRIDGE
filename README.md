# Home Assistant BLE-MQTT Bridge for ELK-BLEDOM Lights

A production-grade Python bridge that integrates cheap, ELK-BLEDOM based BLE RGB light strips into Home Assistant via MQTT. This project provides a stable, resilient service that automatically handles the flaky nature of the hardware.

## The Journey: From Hack to Stable Service

This project began as a experiment to see if it was possible to manage and control the light strip in Home Assistant, but since it has no official integration, it then turned to reverse-engineering the communication between device and the app it belonged to.

1.  **Phase 1: The `gatttool` Hack:** Using the deprecated `gatttool` utility, we intercepted and identified the raw byte commands needed to control the light's power, color, and brightness. An initial script was built around this, but it suffered from extreme instability.

2.  **Phase 2: The `bleak` Refactor:** The script was re-architected from the ground up using `bleak`, a modern, asynchronous Python library. This enabled a persistent connection but revealed a "deep sleep" bug in the controller's firmware where it would become unresponsive after being turned off.

3.  **Phase 3: The "Aggressive Wake-Up":** The final breakthrough was to combine the stability of `bleak` with the brute-force nature of the original hack. The script was engineered with a "software power cycle" that forces a full, aggressive reconnection *only* when turning the light on from an off state, reliably shocking the controller awake.

## Features

* **Stable, Persistent Connection:** Uses `bleak` to maintain a connection, with exponential backoff for reconnection attempts.
* **"Aggressive Wake-Up":** Reliably turns the light on from an `OFF` state by forcing a reconnect.
* **State Reconciliation:** Remembers the last command from Home Assistant and restores it upon reconnection.
* **HA Availability:** Reports `online`/`offline` status to Home Assistant for a seamless UI experience.
* **Secure:** Loads all sensitive information (MAC address, MQTT credentials) from a `secrets.yaml` file.

## How It Works: The Architecture

This bridge works by creating a "translator" that sits between Home Assistant's world of MQTT messages and the light strip's world of Bluetooth commands.

The **MQTT Broker** acts as a central post office. Home Assistant drops off a letter (a JSON command), and the Python script picks it up, translates it, and delivers it to the light strip via Bluetooth.

**Flow of a Command:**
`Home Assistant UI -> MQTT Broker -> Python Script -> Bluetooth Adapter -> Light Strip`


MQTT Setup
This script requires a running MQTT broker. The script connects to the broker, subscribes to a command topic, and publishes state updates. All MQTT configuration is handled in the secrets.yaml file.

Create a secrets.yaml file in the same directory as the script with the following format:

# Your private credentials and configuration
# This file should be added to .gitignore and NOT committed to your repository.

mqtt_broker: "127.0.0.1" # IP address of your MQTT broker
mqtt_port: 1883
mqtt_username: "your_mqtt_user"
mqtt_password: "your_mqtt_password"

device_mac: "BE:67:00:5B:04:4A" # MAC address of your BLE device
base_topic: "bedframe/light" # The base topic for Home Assistant integration




## Deployment

This script is designed to run 24/7 as a `systemd` service on a Linux host (like a Raspberry Pi) that also runs the MQTT broker and Home Assistant. Containerizing the script and broker using `docker-compose` is the recommended next step for a full DevOps deployment.

#### Final Project Structure:
```
ble-mqtt-bridge/
├── .git/
├── venv/
├── .gitignore
├── ble_mqtt_bridge.py  # The main Python script
├── requirements.txt
├── secrets.yaml        # Your private credentials
└── ble-mqtt-bridge.service # The systemd service file
```

## Home Assistant Integration

This bridge creates a standard MQTT Light entity in Home Assistant.

```yaml
# In configuration.yaml
mqtt:
  light:
    - name: "Bedframe LED Light"
      unique_id: "bedframe_led_001"
      schema: json
      state_topic: "bedframe/light/state"
      command_topic: "bedframe/light/set"
      availability_topic: "bedframe/light/availability"
      payload_available: "online"
      payload_not_available: "offline"
      supported_color_modes: ["rgb"]
      brightness: true
      optimistic: false
```

## Device Discovery (for new devices)

To find the MAC address and characteristic handle for a new ELK-BLEDOM device:

1.  **Find MAC Address:** Use `bluetoothctl scan on`.
2.  **Find Characteristic Handle:** Run `sudo gatttool -t public -b <DEVICE_MAC> --char-desc`. The handle is on the line with the UUID `0000fff3-0000-1000-8000-00805f9b34fb`.
