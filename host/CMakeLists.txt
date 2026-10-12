# PC build of the occupancy decision core: replay harness and tests.
#   cmake -S host -B build/host && cmake --build build/host && ctest --test-dir build/host
cmake_minimum_required(VERSION 3.22)
project(occupancy_host C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

set(CORE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../components/occupancy_core)

add_library(occupancy_core STATIC ${CORE_DIR}/src/occupancy_core.c)
target_include_directories(occupancy_core PUBLIC ${CORE_DIR}/include)
target_link_libraries(occupancy_core PUBLIC m)

# Match the firmware build: no FMA contraction, strict warnings.
target_compile_options(occupancy_core PRIVATE -ffp-contract=off -Wall -Wextra -Werror)

add_executable(occ_replay replay.c csi_line.c)
target_link_libraries(occ_replay PRIVATE occupancy_core m)
target_compile_options(occ_replay PRIVATE -Wall -Wextra -Werror)

enable_testing()

add_executable(occ_tests tests/test_core.c csi_line.c)
target_link_libraries(occ_tests PRIVATE occupancy_core m)
target_include_directories(occ_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(occ_tests PRIVATE -Wall -Wextra -Werror)

add_test(NAME occupancy_core COMMAND occ_tests)
