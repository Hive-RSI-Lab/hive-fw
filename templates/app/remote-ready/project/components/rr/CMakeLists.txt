# The remote-ready kit: the test API, the signed remote channel to the
# Hive's bench, updates over it and the trial boot that rolls a bad image
# back. The rr_*.c files without ESP-IDF calls are host-tested
# (test/host/test_rr.c); rr_relay.c and rr_ws.c tie them to the board.
set(requires nvs_flash esp_wifi esp_netif app_update esp-tls mbedtls tcp_transport console esp_timer)
# IDF 4 has libsodium built in; IDF 5 gets it from the component registry
# (idf_component.yml), which adds it to this component's requirements
if(IDF_VERSION_MAJOR LESS 5)
	list(APPEND requires libsodium)
else()
	list(APPEND requires esp_app_format)
endif()

idf_component_register(
	SRCS rr_api.c rr_b64.c rr_boot.c rr_json.c rr_ota.c rr_remote.c rr_sha256.c
	     rr_ws_proto.c rr_ws.c rr_relay.c
	INCLUDE_DIRS include
	REQUIRES ${requires})

# The Hive's public signing key(s) (hex, comma-separated) from the build's
# environment: `export HIFI_HIVE_PUBKEY=...` before `fw build`. Never in git.
# Without it the board connects and reports but refuses every command.
if(DEFINED ENV{HIFI_HIVE_PUBKEY} AND NOT "$ENV{HIFI_HIVE_PUBKEY}" STREQUAL "")
	target_compile_definitions(${COMPONENT_LIB} PRIVATE RR_HIVE_PUBKEY="$ENV{HIFI_HIVE_PUBKEY}")
	message(STATUS "Hive key: set")
else()
	message(STATUS "Hive key: missing (HIFI_HIVE_PUBKEY): the board will refuse remote commands")
endif()

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wno-format-truncation)
