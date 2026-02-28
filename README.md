# OpenDisplay integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/OpenDisplay/Home_Assistant_Integration?style=for-the-badge)](https://github.com/OpenDisplay/Home_Assistant_Integration/releases)
[![GitHub issues](https://img.shields.io/github/issues/OpenDisplay/Home_Assistant_Integration?style=for-the-badge)](https://github.com/OpenDisplay/Home_Assistant_Integration/issues)
![Discord](https://img.shields.io/discord/1453066942544875593?style=for-the-badge)



Home Assistant Integration for the [OpenDisplay](https://opendisplay.org/) project, enabling control and monitoring of E-Paper displays through Home Assistant.

## Requirements

Link to seeed OpenDisplay Wiki page will be added here...

### Hardware
OpenDisplay-compatible Boards/Displays:
 - See [Compatibility Guide](https://opendisplay.org/firmware/seeed_display_compatibility.html)

### 🎨 Display Controls

#### drawcustom (Recommended)
The most flexible and powerful service for creating custom displays. Supports:
- Text with multiple fonts and styles
- Shapes (rectangles, circles, lines)
- Icons from Material Design Icons
- QR codes
- Images from URLs
- Plots of Home Assistant sensor data
- Progress bars

[View full drawcustom documentation](docs/drawcustom/supported_types.md)


## Installation


### Option 1: HACS Installation (Recommended)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=OpenDisplay&repository=Home_Assistant_Integration)

### Option 2: Manual Installation
1. Download the `opendisplay` folder from the [latest release](https://github.com/OpenDisplay/Home_Assistant_Integration/releases/latest)
2. Copy it to your [`custom_components` folder](https://developers.home-assistant.io/docs/creating_integration_file_structure/#where-home-assistant-looks-for-integrations)
3. Restart Home Assistant

## Configuration

Devices should be automatically discovered after installation.


## Usage Examples

### Basic Text Display
```yaml
- type: "text"
  value: "Hello World!"
  x: 10
  y: 10
  size: 40
  color: "red"
```

### Progress Bar with Icon
```yaml
- type: "progress_bar"
  x_start: 10
  y_start: 10
  x_end: 180
  y_end: 30
  progress: 75
  fill: "red"
  show_percentage: true
- type: "icon"
  value: "mdi:battery-70"
  x: 190
  y: 20
  size: 24
```

### Sensor Display
```yaml
- type: "text"
  value: "Temperature: {{ states('sensor.temperature') }}°C"
  x: 10
  y: 10
  size: 24
  color: "black"
- type: "text"
  value: "Humidity: {{ states('sensor.humidity') }}%"
  x: 10
  y: 40
  size: 24
  color: "black"
```

## Contributing
- Feature requests and bug reports are welcome! Please open an issue on GitHub
- Pull requests are encouraged
- Join the [Discord server](https://discord.com/invite/tw48NCrRxH) to discuss ideas and get help
