# Main unit: receives the room receivers' reports and listens to the room
# transmitters. link_config (per-link radio identity) and the occupancy core
# (motion statistic) are shared with csi-rx/tx.
cmake_minimum_required(VERSION 3.22)

set(EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../components)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(csi-main)
