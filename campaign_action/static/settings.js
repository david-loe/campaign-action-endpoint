"use strict";
const fieldList = document.getElementById("field-list");
const addButton = document.getElementById("add-field");

function updateFields() {
  const fields = [...fieldList.querySelectorAll(".field-editor")];
  fields.forEach((field, index) => {
    field.querySelector("legend").textContent = `Formularfeld ${index + 1}`;
    const up = field.querySelector('[data-move="up"]');
    const down = field.querySelector('[data-move="down"]');
    if (up) up.disabled = index === 0;
    if (down) down.disabled = index === fields.length - 1;
  });
  if (addButton) addButton.disabled = fields.length >= 50;
}

if (addButton) {
  addButton.addEventListener("click", () => {
    fieldList.append(document.getElementById("field-template").content.cloneNode(true));
    updateFields();
    fieldList.lastElementChild.querySelector('input[name="field_label"]').focus();
  });
}

if (fieldList && fieldList.dataset.locked !== "true") {
  fieldList.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const field = button.closest(".field-editor");
    if (button.hasAttribute("data-remove")) field.remove();
    if (button.dataset.move === "up" && field.previousElementSibling) {
      fieldList.insertBefore(field, field.previousElementSibling);
    }
    if (button.dataset.move === "down" && field.nextElementSibling) {
      fieldList.insertBefore(field.nextElementSibling, field);
    }
    updateFields();
  });
}
if (fieldList) updateFields();
