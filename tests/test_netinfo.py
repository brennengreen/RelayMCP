from relaymcp.host import netinfo


def test_normalize_mac():
    assert netinfo.normalize_mac("aa:bb:cc:11:22:33") == "AA-BB-CC-11-22-33"
    assert netinfo.normalize_mac("0:1b:2:a:b:c") == "00-1B-02-0A-0B-0C"
    assert netinfo.normalize_mac("ff:ff:ff:ff:ff:ff") is None
    assert netinfo.normalize_mac("not a mac") is None


def test_parse_gateway_ip():
    mac_route = "   route to: default\ndestination: default\n    gateway: 10.0.0.1\n  interface: en0\n"
    assert netinfo.parse_gateway_ip(mac_route) == "10.0.0.1"
    assert netinfo.parse_gateway_ip("default via 192.168.1.1 dev wlan0 proto dhcp metric 600") == "192.168.1.1"
    assert netinfo.parse_gateway_ip("192.168.50.1\r\n") == "192.168.50.1"
    assert netinfo.parse_gateway_ip("") is None


def test_parse_neighbor_mac():
    assert netinfo.parse_neighbor_mac("? (10.0.0.1) at aa:bb:cc:11:22:33 on en0 ifscope [ethernet]") == "AA-BB-CC-11-22-33"
    assert netinfo.parse_neighbor_mac("192.168.1.1 dev wlan0 lladdr 00:11:22:33:44:55 REACHABLE") == "00-11-22-33-44-55"
    assert netinfo.parse_neighbor_mac("AA-BB-CC-11-22-33") == "AA-BB-CC-11-22-33"
    assert netinfo.parse_neighbor_mac("? (10.0.0.1) at (incomplete) on en0") is None
