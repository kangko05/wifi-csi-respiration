idf_component_register(SRCS "csi-tx.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event esp_timer nvs_flash link_config)
