from pathlib import Path
from xml.etree import ElementTree

from rosmon2 import __version__


def test_python_and_ros_package_versions_match():
    package_xml = Path(__file__).parents[1] / 'package.xml'
    ros_version = ElementTree.parse(package_xml).getroot().findtext('version')

    assert ros_version == __version__


def test_completion_hook_is_declared_for_colcon_environment():
    colcon_metadata = Path(__file__).parents[1] / 'colcon.pkg'
    assert 'share/rosmon2/environment/rosmon2-argcomplete.bash' in (
        colcon_metadata.read_text()
    )

    package_xml = Path(__file__).parents[1] / 'package.xml'
    manifest = ElementTree.parse(package_xml).getroot()
    dependencies = {
        node.text for node in manifest.findall('exec_depend')
    }
    assert 'python3-argcomplete' in dependencies
