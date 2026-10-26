idf_component_register(SRCS "csi-main.c" "../../csi-rx/main/ari2_packet.c"
                       INCLUDE_DIRS "." "../../csi-rx/main"
                       PRIV_REQUIRES esp_driver_uart esp_wifi esp_netif lwip esp_event esp_timer nvs_flash link_config occupancy_core)
