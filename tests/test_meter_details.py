"""Connection and meter details, as diagnostic sensors.

The service point response names the distributor, the loss factor applied to
network charges, and each meter's installation and read type. None of it
changes the imported numbers, but it is what a distributor or retailer asks
for when something needs sorting out.
"""

from __future__ import annotations

import pytest
from conftest import load
from fake_client import async_setup_integration, sensor_state
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.first_energy.api.parsers import parse_service_point
from custom_components.first_energy.const import DOMAIN
from custom_components.first_energy.sensor import DIAGNOSTIC_SENSORS


class TestParsing:
    def test_reads_the_distributor_and_loss_factor(self):
        sp = parse_service_point(load("servicepoint"))
        assert sp.distributor == "Example Networks"
        assert sp.loss_factor == pytest.approx(1.058)
        assert sp.loss_factor_code == "XX0A"
        assert sp.loss_factor_description == "Low Voltage Urban"

    def test_reads_the_meter_specifications(self):
        sp = parse_service_point(load("servicepoint"))
        meter = sp.consumption_meter
        assert meter is not None
        assert meter.meter_id == "M1"
        assert meter.installation_type == "COMMS4D"
        assert meter.read_type == "RWDA"

    def test_the_network_tariff_comes_from_the_live_register(self):
        assert parse_service_point(load("servicepoint")).network_tariff_code == "N70"

    def test_they_are_optional(self):
        sp = parse_service_point({"data": {"nationalMeteringId": "1",
                                           "relatedParticipants": "nope"}})
        assert sp.distributor is None
        assert sp.loss_factor is None
        assert sp.consumption_meter is None
        assert sp.network_tariff_code is None

    def test_only_the_network_operator_counts_as_the_distributor(self):
        payload = {"data": {"nationalMeteringId": "1", "relatedParticipants": [
            {"party": "1st Energy", "role": "FRMP"}]}}
        assert parse_service_point(payload).distributor is None


@pytest.fixture
async def loaded(recorder_mock, enable_custom_integrations, hass: HomeAssistant):
    return await async_setup_integration(hass)


class TestSensors:
    async def test_values(self, loaded, hass: HomeAssistant):
        assert sensor_state(hass, "nmi").state == "9999990001"
        assert sensor_state(hass, "distributor").state == "Example Networks"
        assert sensor_state(hass, "network_tariff").state == "N70"
        assert float(sensor_state(hass, "loss_factor").state) == pytest.approx(1.058)
        assert sensor_state(hass, "meter").state == "M1"

    async def test_attributes(self, loaded, hass: HomeAssistant):
        assert sensor_state(hass, "loss_factor").attributes["code"] == "XX0A"
        meter = sensor_state(hass, "meter").attributes
        assert meter["installation_type"] == "COMMS4D"
        assert meter["registers"] == ["E1"]

    async def test_they_are_diagnostic(self, loaded, hass: HomeAssistant):
        registry = er.async_get(hass)
        for description in DIAGNOSTIC_SENSORS:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"638594_{description.key}")
            assert entity_id is not None
            entry = registry.async_get(entity_id)
            assert entry is not None
            assert entry.entity_category is EntityCategory.DIAGNOSTIC
