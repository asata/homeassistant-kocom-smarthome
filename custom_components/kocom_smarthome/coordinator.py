import re
from datetime import datetime
from datetime import timedelta

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    VERSION,
    LOGGER,
    DEFAULT_TEMP_RANGE,
    ELEMENT_INFO,
    ELEMENT_UNITNAME
)
from .api import parse_device_info


class KocomSmartHomeCoordinator(DataUpdateCoordinator):
    """Kocom Smart Home update coordinator."""

    def __init__(self, name, api, hass, entry) -> None:
        """Initializes coordinator."""
        self.name = name
        self.api = api
        self.hass = hass
        self.entry = entry
        self._irdev = False
        name_interval = f"{name}_interval"
        update_interval = entry.data[name_interval]

        if name in ["light", "concent", "heat", "aircon"]:
            self._irdev = True
            self._device_info = api.device_settings[name]
        else:
            self._device_info = {"data": {}, "sync_date": ""}

        super().__init__(
            hass, LOGGER, name=name, update_interval=timedelta(seconds=update_interval)
        )
    
    async def get_energy_usage(self) -> dict:
        """Fetches and updates energy usage data."""
        energy_usage = await self.api.fetch_energy_stdcheck()
        
        if energy_usage is None:
            LOGGER.warning("Energy usage fetch returned None, keeping existing data")
            return self._device_info

        # Midnight debug logging to investigate month transition
        now = datetime.now()
        if now.hour == 0:
            LOGGER.info(
                "[Midnight Transition Data] Time: %s, Response: %s",
                now.strftime("%H:%M:%S"),
                energy_usage
            )

        if "error" in energy_usage and energy_usage.get("error") != 0:
            LOGGER.warning(
                "Energy usage API error: %s (code: %s)",
                energy_usage.get("error-msg", "Unknown Error"),
                energy_usage.get("error", "Unknown")
            )
            return self._device_info

        usage_list = energy_usage.get("list")
        if not usage_list:
            LOGGER.warning("Energy usage response missing 'list' key or list is empty: %s", energy_usage)
            return self._device_info

        # Month-aware data integrity check for electricity to prevent glitches
        new_elec = next((e for e in usage_list if e.get("energy") == "elec"), None)
        if new_elec:
            new_val = new_elec.get("value", 0)
            new_date = str(new_elec.get("date", "")).split()[0] # YYYY-MM-DD or YYYY-MM
            
            old_usage_list = self._device_info.get("data", {}).get("list", [])
            old_elec = next((e for e in old_usage_list if e.get("energy") == "elec"), None)
            
            if old_elec:
                old_val = old_elec.get("value", 0)
                old_date = str(old_elec.get("date", "")).split()[0]
                
                # Compare only if the month is the same (comparing YYYY-MM)
                if new_date[:7] == old_date[:7] and new_date[:7] != "":
                    if new_val < old_val:
                        LOGGER.warning(
                            "Electricity usage decreased within the same month (%s -> %s, date: %s). "
                            "Suspecting API glitch, keeping existing data.",
                            old_val, new_val, new_date
                        )
                        return self._device_info

        self._device_info.update({
            "data": energy_usage,
            "sync_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        return self._device_info

    async def get_single_device(self, ctrl_resp=None) -> dict:
        """Gets status for a single device."""
        if ctrl_resp:
            device_state = ctrl_resp
        else:
            device_state = await self.api.check_device_status(self.name)

        if device_state is None:
            LOGGER.warning("Device status fetch returned None for %s, keeping existing data", self.name)
            return self._device_info

        if device_state.get("type") == "totalcontrol":
            entries = device_state.get("entry", [])
            if entries and entries[0].get("list"):
                # 0: totalcontrol, 1: totalelevator
                entry_to_list = entries[0]["list"][0]
                entry_to_list["value"] = ~int(entry_to_list.get("value", 0)) & 1
            else:
                LOGGER.debug("Totalcontrol entry or list is missing in device_state")

        power_key = "totallight" if self.name == "totalcontrol" else "power"
        data_updates = {
            "attr": parse_device_info(device_state, "attr"),
            "power": parse_device_info(device_state, power_key),
        }
        if self.name == "vent":
            data_updates["wind"] = parse_device_info(device_state, "wind")

        self._device_info["data"].update(data_updates)
        self._update_sync_date()

        return self._device_info
    
    def _energy_usage_state(self, unique_id: str, target_date: str) -> dict:
        """Returns energy usage state for given ID and date."""
        pattern = r"(elec|gas|water|hotwater|heat).*?(value|avg|price)"
        match = re.search(pattern, unique_id)
        if not match:
            LOGGER.warning("Invalid energy usage ID format: %s", unique_id)
            return None

        energy_type, data_type = match.group(1), match.group(2)
        data = self._device_info.get("data")
        if not data or "list" not in data:
            LOGGER.debug("Energy data not yet available for %s", unique_id)
            return None

        for data_entry in data["list"]:
            if (data_entry["energy"] == energy_type
                and data_entry["date"] == target_date
            ):
                return data_entry.get(data_type)

        LOGGER.debug(
            "No matching energy entry found for type=%s, date=%s (id=%s)",
            energy_type, target_date, unique_id
        )
        return None
    
    def get_device_status(self, unique_id: str = None, function: str = "power") -> bool:
        """Returns device power state."""
        if self._irdev and unique_id:
            id_parts = unique_id.split("-")[0].split("_")
            id = id_parts[0].title()
            if id_parts[1] == "00":
                id_parts[1] = function
            return self.api.current_device_state(self.name, id, id_parts[1])
        else:
            return self._device_info.get("data", {}).get(function)
        
    def _interpret_command(self, unique_id: str, value: int, function: str) -> tuple:
        """Processes device command parameters."""
        id_parts = unique_id.split("-")[0].split("_")
        id = id_parts[0].title()

        if unique_id.startswith(("vent", "gas", "totalcontrol")):
            if unique_id.startswith("totalcontrol"):
                value = ~value & 1
                function = "totallight"
        elif unique_id.startswith(("lt", "ct")):
            value *= 255
            function = id_parts[1]

        return id, function, value

    def _is_previous_month(self, date_str: str) -> bool:
        """Checks if date is from previous month."""
        try:
            previous_month = int(date_str.split()[0].replace("-", "")) // 100
            current_year_month = int(datetime.now().strftime("%Y%m"))
            return current_year_month > previous_month
        except Exception:
            return False
        
    def _update_sync_date(self):
        """Updates device sync timestamp."""
        self._device_info.update({
            "sync_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    async def _async_update_data(self) -> None:
        """Updates device data based on type."""
        if self.name in ["gas", "vent", "totalcontrol"]:
            return await self.update_single_device()
        elif self.name == "energy":
            return await self.update_energy_usage()
        else:
            return await self.update_room_device()
    
    async def set_device_command(
        self, unique_id: str, value: int, function: str = "power"
    ) -> None:
        """Sends control command to device."""
        id, function, value = self._interpret_command(unique_id, value, function)
        ctrl_resp = await self.api.send_control_request(self.name, id, function, value)

        if ctrl_resp is None:
            LOGGER.warning(
                "Control request returned None for %s (id=%s, function=%s), skipping state update",
                self.name, id, function
            )
            return

        if self._irdev:
            self.api.update_device_data(ctrl_resp)
        else:
            await self.get_single_device(ctrl_resp)

        await self.async_request_refresh()

    async def specify_elements(self) -> list:
        """Returns list of energy usage elements."""
        energy_usage = await self.get_energy_usage()
        devices = []

        usage_list = energy_usage.get("data", {}).get("list")
        if not usage_list:
            LOGGER.warning("Energy usage list is empty or unavailable, returning no elements")
            return devices

        for usage_device_info in usage_list:
            is_prev_month = self._is_previous_month(usage_device_info["date"])
            prev_suffix = "_previous" if is_prev_month else ""
            energy_type = usage_device_info["energy"]

            for key, value in usage_device_info.items():
                if key in ["energy", "date"]:
                    continue

                entry_device_info = {
                    "device_id": f"{energy_type}{prev_suffix}_{key}_usage" 
                    if key != "price" 
                    else f"{energy_type}{prev_suffix}_expect_price",
                    "device_name": f"{ELEMENT_UNITNAME[f'{prev_suffix}{key}']} {ELEMENT_INFO[energy_type][0]}" 
                    if key != "price" else 
                    f"{ELEMENT_INFO[energy_type][0]} {ELEMENT_UNITNAME[f'{prev_suffix}price']}",
                    "device_icon": ELEMENT_INFO[energy_type][1],
                    "device_unit": ELEMENT_INFO[energy_type][2] if key != "price" else "KRW/kWh",
                    "device_class": ELEMENT_INFO[energy_type][3],
                    "state_class": ELEMENT_INFO[energy_type][4],
                    "device_room": usage_device_info["energy"],
                    "device_type": key,
                    "is_prev_month": is_prev_month,
                    "reg_date": usage_device_info["date"]
                }       
                entry_device_info["device_id"] += f"-{self.entry.data['phone_number']}"
                devices.append(entry_device_info)

        LOGGER.debug(
            "Found %d energy usage elements: %s", len(devices), 
            [f"{d['device_name']} ({d['device_id']})" for d in devices]
        )
        return devices

    async def get_devices(self) -> list:
        """Returns list of available devices."""
        devices = []
        if self.name == "energy":
            devices = await self.specify_elements()
        elif self.name in ["gas", "vent", "totalcontrol"]:
            single_device = await self.get_single_device()
            entry_device_info = {
                "device_id": f"{single_device['data']['attr']['id'].lower()}_00",
                "device_name": {"gas": "가스", "vent": "환기", "totalcontrol": "일괄소등"}[self.name],
                "device_room": "00",
                "device_type": single_device["data"]["attr"]["type"],
                "reg_date": single_device["data"]["attr"]["reg_date"]
            }
            entry_device_info["device_id"] += f"-{self.entry.data['phone_number']}"
            devices.append(entry_device_info)
        else:
            for entry in self._device_info["data"]["entry"]:
                for entry_list in entry["list"]:
                    device_id = entry.get("id", "").lower()
                    function = entry_list.get("function", "")
                    device_name = f"{entry.get('id', '')} {function}" if function else f"{entry.get('id', '')} 00"
                    device_room = entry.get("id", "")[-2:] if function else "00"
                    device_type = self._device_info["data"]["type"]
                    reg_date = entry.get("reg_date", "")

                    if device_type in ["heat", "aircon"]:
                        devices.append({
                            "device_id": f"{device_id}_00-{self.entry.data['phone_number']}",
                            "device_name": device_name,
                            "device_room": device_room,
                            "device_type": device_type,
                            "reg_date": reg_date,
                            **DEFAULT_TEMP_RANGE.get(device_type, {})
                        })
                    else:
                        devices.append({
                            "device_id": f"{device_id}_{function}-{self.entry.data['phone_number']}",
                            "device_name": device_name,
                            "device_room": device_room,
                            "device_type": device_type,
                            "reg_date": reg_date
                        })

        LOGGER.debug(
            "Found %d devices for %s: %s", len(devices), self.name,
            [f"{d['device_name']} ({d['device_id']})" for d in devices]
        )
        return devices

    async def update_single_device(self) -> None:
        """Updates single device status."""
        return await self.get_single_device()
    
    async def update_energy_usage(self) -> None:
        """Updates energy usage data."""
        return await self.get_energy_usage()
        
    async def update_room_device(self) -> None:
        """Updates room device state."""
        return await self.api.update_device_state(self.name)

    def get_device_info(self) -> DeviceInfo:
        """Returns device information."""
        is_specific_name = self.name in ["gas", "vent", "totalcontrol", "room"]
        return DeviceInfo(
            identifiers={(DOMAIN, "kocom" if is_specific_name else self.name)},
            name="KOCOM" if is_specific_name else f"KOCOM {self.name.title()}",
            manufacturer="Kocom Co, Ltd.",
            model=self.api.user_credentials["pairing_info"]["alias"],
            sw_version=VERSION,
        )