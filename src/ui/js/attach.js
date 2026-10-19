// Картинки в поле ввода: вставка из буфера (Ctrl+V), перетаскивание и кнопка «＋».
// Картинка сразу уходит на сервер, а в сообщение попадает только её имя.
import { api } from "./api.js";
import { el } from "./chat.js";

// Длинная сторона больших фото уменьшается: модель всё равно сжимает картинку, а в каждый запрос она идёт целиком.
const MAX_SIDE = 2048;
const KEPT_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

// box — рамка поля ввода (туда можно бросить файл), strip — полоска миниатюр над строкой ввода.
export function imageInput({ box, input, strip, button, toast }) {
  let items = [];
  const picker = el("input");
  Object.assign(picker, { type: "file", accept: "image/*", multiple: true, hidden: true });
  box.append(picker);

  const draw = () => {
    strip.hidden = !items.length;
    strip.replaceChildren(...items.map((item) => item.node));
  };
  const drop = (item) => {
    URL.revokeObjectURL(item.preview);
    items = items.filter((other) => other !== item);
    draw();
  };
  const add = (file) => {
    if (!file.type.startsWith("image/")) return;
    const item = { name: null, preview: URL.createObjectURL(file), node: el("span", "thumb loading") };
    const image = el("img");
    Object.assign(image, { src: item.preview, alt: file.name || "картинка" });
    const remove = el("button", "thumb-remove", "×");
    Object.assign(remove, { type: "button", title: "Убрать картинку" });
    remove.addEventListener("click", () => drop(item));
    item.node.append(image, remove);
    item.ready = shrink(file)
      .then((data) => api.attach(data))
      .then(({ name }) => {
        item.name = name;
        item.node.classList.remove("loading");
      })
      .catch((error) => {
        toast(error);
        drop(item);
      });
    items.push(item);
    draw();
  };
  const addAll = (files) => {
    for (const file of files) add(file);
  };

  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files ?? [])].filter((file) => file.type.startsWith("image/"));
    if (!files.length) return;
    event.preventDefault();
    addAll(files);
  });
  box.addEventListener("dragover", (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
  box.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files.length) return;
    event.preventDefault();
    addAll(event.dataTransfer.files);
  });
  button.addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    addAll(picker.files);
    picker.value = "";
  });

  return {
    get count() {
      return items.length;
    },
    // Имена загруженных картинок; если какие-то ещё грузятся — дожидается их.
    async names() {
      await Promise.all(items.map((item) => item.ready));
      return items.map((item) => item.name).filter(Boolean);
    },
    clear() {
      for (const item of items) URL.revokeObjectURL(item.preview);
      items = [];
      draw();
    },
  };
}

async function shrink(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && KEPT_TYPES.includes(file.type)) {
    bitmap.close();
    return read(file);
  }
  const canvas = el("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  // Скриншоты — без потерь, чтобы мелкий текст остался читаемым; фото — JPEG.
  return canvas.toDataURL(file.type === "image/jpeg" ? "image/jpeg" : "image/png", 0.9);
}

function read(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
