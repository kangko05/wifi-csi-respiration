# The occupancy decision core is a shared component (also built by host/).
cmake_minimum_required(VERSION 3.22)

set(EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../components)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(csi-rx)
