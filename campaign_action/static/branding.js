"use strict";

document.querySelectorAll("[data-color-control]").forEach((control) => {
  const picker = control.querySelector("[data-color-picker]");
  const hex = control.querySelector("[data-color-hex]");
  picker.addEventListener("input", () => { hex.value = picker.value; });
  hex.addEventListener("input", () => {
    if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) picker.value = hex.value;
  });
});

const brandingMode = document.getElementById("branding-mode");
const customBranding = document.getElementById("custom-branding");
if (brandingMode && customBranding) {
  const updateBranding = () => {
    const useGlobal = brandingMode.value === "global";
    customBranding.hidden = useGlobal;
    customBranding.disabled = useGlobal;
  };
  brandingMode.addEventListener("change", updateBranding);
  updateBranding();
}
