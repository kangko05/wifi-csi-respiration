# Header-only component: the per-link radio identity shared by csi-tx and csi-rx.
idf_component_register(INCLUDE_DIRS "include")

# Select the link at configure time: idf.py -DCSI_LINK_ID=1 -B build/link1 build
if(NOT DEFINED CSI_LINK_ID)
    set(CSI_LINK_ID 0)
endif()

target_compile_definitions(${COMPONENT_LIB} INTERFACE CSI_LINK_ID=${CSI_LINK_ID})
