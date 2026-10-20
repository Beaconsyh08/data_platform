// Deliberately small Markdown subset. Model text never becomes HTML, attributes or links.
function assistantFormattedText(value) {
  const body = document.createElement('div'); body.className = 'assistant-formatted';
  function inline(parent, text, depth = 0) {
    if (depth > 2) {parent.append(document.createTextNode(text)); return;}
    const tokens = /`([^`\n]+)`|\*\*([^\n]+?)\*\*/g;
    let offset = 0;
    for (const match of text.matchAll(tokens)) {
      parent.append(document.createTextNode(text.slice(offset, match.index)));
      const node = document.createElement(match[1] !== undefined ? 'code' : 'strong');
      if (match[1] !== undefined) node.textContent = match[1]; else inline(node, match[2], depth + 1);
      parent.append(node); offset = match.index + match[0].length;
    }
    parent.append(document.createTextNode(text.slice(offset)));
  }
  let paragraph = null, list = null, fence = null;
  for (const line of String(value ?? '').replace(/\r\n?/g, '\n').split('\n')) {
    if (/^\s*```/.test(line)) {
      if (fence) fence = null;
      else {const pre = document.createElement('pre'); fence = document.createElement('code'); pre.append(fence); body.append(pre);}
      paragraph = list = null; continue;
    }
    if (fence) {fence.append(document.createTextNode(line + '\n')); continue;}
    if (!line.trim()) {paragraph = list = null; continue;}
    const item = line.match(/^\s*(?:(\d+)[.)]|[-*+])\s+(.+)$/);
    if (item) {
      const type = item[1] ? 'OL' : 'UL';
      if (!list || list.tagName !== type) {
        list = document.createElement(type.toLowerCase()); body.append(list);
        if (item[1]) list.start = Math.min(1000000, Number(item[1]));
      }
      const li = document.createElement('li'); inline(li, item[2]); list.append(li); paragraph = null;
    } else {
      list = null;
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      if (heading) {
        const title = document.createElement('p'); title.className = 'assistant-heading'; inline(title, heading[1]); body.append(title); paragraph = null;
      } else {
        if (!paragraph) {paragraph = document.createElement('p'); body.append(paragraph);} else paragraph.append(document.createElement('br'));
        inline(paragraph, line);
      }
    }
  }
  return body;
}
