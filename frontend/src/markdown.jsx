// Minimal, safe markdown rendering for assistant replies.
// Builds React elements only (no innerHTML), so model output can never inject markup.
// Supports: **bold**, *italic* / _italic_, `inline code`, "- " / "* " bullets, "1. " lists, # headings.

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\s][^*\n]*\*|_[^_\s][^_\n]*_)/g;

function renderInline(text, keyPrefix) {
  const parts = text.split(INLINE);
  return parts.map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={key}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={key}>{part.slice(1, -1)}</code>;
    if ((part.startsWith("*") && part.endsWith("*")) || (part.startsWith("_") && part.endsWith("_"))) {
      if (part.length > 2) return <em key={key}>{part.slice(1, -1)}</em>;
    }
    return part;
  });
}

export function Markdown({ text }) {
  const lines = text.split("\n");
  const blocks = [];
  let list = null;

  const flush = () => {
    if (list) {
      const Tag = list.ordered ? "ol" : "ul";
      blocks.push(<Tag key={`l${blocks.length}`}>{list.items}</Tag>);
      list = null;
    }
  };

  lines.forEach((line, idx) => {
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
    if (bullet || ordered) {
      const isOrdered = Boolean(ordered);
      if (!list || list.ordered !== isOrdered) {
        flush();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push(<li key={idx}>{renderInline((bullet || ordered)[1], `li${idx}`)}</li>);
      return;
    }
    flush();
    if (heading) {
      blocks.push(<p key={idx} className="md-heading">{renderInline(heading[1], `h${idx}`)}</p>);
    } else if (line.trim() === "") {
      blocks.push(<div key={idx} className="md-gap" />);
    } else {
      blocks.push(<p key={idx}>{renderInline(line, `p${idx}`)}</p>);
    }
  });
  flush();
  return <div className="md">{blocks}</div>;
}
