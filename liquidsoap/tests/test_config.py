from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def test_icecast_config_is_valid_secret_free_xml():
    config_path = ROOT / "config" / "icecast.xml"
    text = config_path.read_text(encoding="utf-8")
    root = ET.parse(config_path).getroot()

    assert root.tag == "icecast"
    assert root.findtext("./hostname") == "${ICECAST_HOSTNAME}"
    assert root.findtext("./listen-socket/port") == "${ICECAST_PORT}"
    assert root.findtext("./mount/mount-name") == "/radio.mp3"
    assert root.findtext("./mount/max-listeners") == "50"

    password_nodes = root.findall(".//source-password")
    password_nodes += root.findall(".//relay-password")
    password_nodes += root.findall(".//admin-password")
    assert {node.text for node in password_nodes} == {
        "${ICECAST_SOURCE_PASSWORD}",
        "${ICECAST_RELAY_PASSWORD}",
        "${ICECAST_ADMIN_PASSWORD}",
    }

    lowered = text.lower()
    assert "hackme" not in lowered
    assert "changeme" not in lowered


def test_runtime_paths_are_placeholders_or_system_paths():
    root = ET.parse(ROOT / "config" / "icecast.xml").getroot()
    assert root.findtext("./paths/logdir") == "${RADIO_LOG_DIR}"
    assert root.findtext("./paths/webroot") == "/usr/share/icecast2/web"
    assert root.findtext("./paths/adminroot") == "/usr/share/icecast2/admin"

