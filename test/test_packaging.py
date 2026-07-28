from pathlib import Path
from xml.etree import ElementTree

from rosmon2 import __version__


def test_python_and_ros_package_versions_match():
    package_xml = Path(__file__).parents[1] / 'package.xml'
    ros_version = ElementTree.parse(package_xml).getroot().findtext('version')

    assert ros_version == __version__
