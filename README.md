## 🌈 BLE-MQTT Bridge (Lotus Lantern / ELK-BLEDOM)

    A lightweight Python bridge that lets Home Assistant control BLE RGB LED devices (like Lotus Lantern / ELK-BLEDOM) over MQTT. It translates MQTT JSON commands into BLE packets to control power, color, and brightness.

    ---

    ## 🔧 Features
    - Translates Home Assistant MQTT JSON payloads into BLE RGB commands
    - Supports power on/off, RGB color, and brightness control
    - Auto-reconnects to BLE device and maintains last known state
    - Optional state publishing for HA feedback (commented in code)

    ---

    ## 🚀 Quick Start
    ### 1. Clone & Install Dependencies
    ```bash
    git clone https://github.com/yourusername/ble-mqtt-bridge.git
    cd ble-mqtt-bridge
    pip install -r requirements.txt
    ```

    ### 2. Configure Your Device
    - Edit `bt_mqtt_lotus.py`
      - Set `DEVICE_MAC_ADDRESS`, MQTT IP, topic, and optionally auth

    ### 3. Run the Bridge
    ```bash
    python3 bt_mqtt_lotus.py
    ```

    ---

    ## 🏠 Home Assistant Example
    See [`ha_example.yaml`](ha_example.yaml):
    ```yaml
    light:
      - platform: mqtt
        name: "Bedframe Light"
        schema: json
        command_topic: "bedframe/light/set_json"
        state_topic: "bedframe/light/state_json"
        brightness: true
        rgb: true
    ```

    Make sure the topics and structure match what your script listens to.

    ---

    ## 📦 Requirements
    - Python 3.x
    - `bluepy` (Bluetooth LE control)
    - `paho-mqtt` (MQTT communication)

    ---

    ## 📄 License
    MIT License — see [`LICENSE`](LICENSE) for details.

    ---

    ## 💡 Future Goals
    - Async BLE + MQTT
    - YAML/ENV config support
    - HA Add-on packaging or Dockerization
    - Extended device support

    > Until then, keep it lean and hackable.
