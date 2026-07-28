from glob import glob
import os
from pathlib import Path
from xml.etree import ElementTree

from setuptools import find_packages, setup


package_name = 'rosmon2'
project_root = Path(__file__).parent
long_description = (project_root / 'README.md').read_text(encoding='utf-8')
package_version = (
    ElementTree.parse(project_root / 'package.xml').getroot().findtext('version')
)

setup(
    name=package_name,
    version=package_version,
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'),
         [path for path in glob('launch/*') if os.path.isfile(path)]),
    ],
    install_requires=['setuptools'],
    extras_require={'test': ['pytest']},
    python_requires='>=3.10',
    zip_safe=False,
    maintainer='Gibson',
    maintainer_email='gibson.hu@uts.edu.au',
    description='A rosmon-style terminal launcher and process monitor for ROS 2.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    license='BSD-3-Clause',
    license_files=('LICENSE',),
    url='https://github.com/GibsonHu/rosmon2',
    project_urls={
        'Bug Tracker': 'https://github.com/GibsonHu/rosmon2/issues',
        'Source': 'https://github.com/GibsonHu/rosmon2',
    },
    keywords=('ros ros2 launch process-monitor terminal supervisor'),
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: System :: Monitoring',
    ],
    entry_points={
        'console_scripts': [
            'mon2 = rosmon2.cli:main',
            'rosmon2 = rosmon2.cli:main',
            'rosmon2-mcp = rosmon2.mcp_server:main',
        ],
    },
)
