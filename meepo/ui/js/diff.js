// Построчная разница двух текстов: какие строки файла только что поменял агент (вкладка «Файлы»).
// Таблица сравнения растёт как произведение длин: на огромных файлах разницу не считаем.
const MAX_CELLS = 4_000_000;

// Операции по строкам: { kind: " " | "-" | "+", text, before, after } — before/after — номера строк в старом и новом тексте.
export function lineDiff(before, after) {
  const a = before.split("\n");
  const b = after.split("\n");
  // Одинаковые начало и конец отрезаем сразу: обычно правка меняет пару строк в середине.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }
  const middleA = a.slice(start, endA);
  const middleB = b.slice(start, endB);
  if (middleA.length * middleB.length > MAX_CELLS) return null;
  // Длина общей подпоследовательности с конца: lcs[i][j] для middleA[i:], middleB[j:].
  const width = middleB.length + 1;
  const lcs = new Uint32Array((middleA.length + 1) * width);
  for (let i = middleA.length - 1; i >= 0; i -= 1) {
    for (let j = middleB.length - 1; j >= 0; j -= 1) {
      lcs[i * width + j] = middleA[i] === middleB[j]
        ? lcs[(i + 1) * width + j + 1] + 1
        : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }
  const ops = [];
  for (let index = 0; index < start; index += 1) ops.push({ kind: " ", text: a[index], before: index + 1, after: index + 1 });
  let i = 0;
  let j = 0;
  while (i < middleA.length || j < middleB.length) {
    if (i < middleA.length && j < middleB.length && middleA[i] === middleB[j]) {
      ops.push({ kind: " ", text: middleA[i], before: start + i + 1, after: start + j + 1 });
      i += 1;
      j += 1;
    } else if (i < middleA.length && (j === middleB.length || lcs[(i + 1) * width + j] >= lcs[i * width + j + 1])) {
      // При равенстве сначала удалённая строка, потом добавленная — как в git.
      ops.push({ kind: "-", text: middleA[i], before: start + i + 1, after: null });
      i += 1;
    } else {
      ops.push({ kind: "+", text: middleB[j], before: null, after: start + j + 1 });
      j += 1;
    }
  }
  for (let index = endA; index < a.length; index += 1) {
    ops.push({ kind: " ", text: a[index], before: index + 1, after: index - endA + endB + 1 });
  }
  return ops;
}
