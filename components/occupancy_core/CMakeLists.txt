# ESP-IDF component. The PC harness builds the same sources from host/.
idf_component_register(SRCS "src/occupancy_core.c"
                       INCLUDE_DIRS "include")

# Same rounding on device and host: no fused multiply-add contraction.
target_compile_options(${COMPONENT_LIB} PRIVATE -ffp-contract=off)
