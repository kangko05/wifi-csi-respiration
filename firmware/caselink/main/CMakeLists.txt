idf_component_register(SRCS "caselink.c" "link_config.c" "frame_out.c" "radio.c" "ari2_packet.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash esp_driver_uart)
