idf_component_register(SRCS "csi-rx.c" "ari2_packet.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash esp_driver_uart occupancy_core link_config)
