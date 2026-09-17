set(CSI_RESP_SRCS
    src/preprocess.c
    src/csi_utils.c
    src/amplitude.c
)

if(ESP_PLATFORM)
    idf_component_register(SRCS ${CSI_RESP_SRCS}
                           INCLUDE_DIRS include)
    return()
endif()

cmake_minimum_required(VERSION 3.22)
project(csi_resp C)
set(CMAKE_C_STANDARD 11)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_library(csi_resp STATIC ${CSI_RESP_SRCS})
target_include_directories(csi_resp PUBLIC include)
target_link_libraries(csi_resp PUBLIC m)

add_executable(csi_resp_main src/main.c)
target_link_libraries(csi_resp_main PRIVATE csi_resp)
