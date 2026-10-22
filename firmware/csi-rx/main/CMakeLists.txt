idf_component_register(SRCS "csi-rx.c" "ari2_packet.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash esp_driver_uart occupancy_core link_config)

foreach(flag RX_FORCE_RX_GAIN RX_ARI2_SEND_BROADCAST)
    if(DEFINED ${flag})
        target_compile_definitions(${COMPONENT_LIB} PRIVATE ${flag}=${${flag}})
    endif()
endforeach()
