/**
 * Node palette.
 *
 * Grouped by the backend's `category` and searchable across label, description,
 * and type. Built entirely from the `/api/node-types` response: adding a node
 * type on the backend makes it appear here with no frontend change.
 *
 * Entries are draggable onto the canvas *and* clickable (which drops the node in
 * a free spot). Clicking matters for keyboard and trackpad users, who would
 * otherwise have to drag precisely.
 */

import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';

import { useT, type MessageKey } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';
import { NODE_CATEGORIES, type NodeCategory, type NodeTypeSpec } from '@/types/workflow';
import { Icon } from '@/ui/Icon';

/** Order in which categories appear — roughly the order of a workflow's life. */
const CATEGORY_ORDER: NodeCategory[] = [
  'input',
  'model',
  'integration',
  'data',
  'logic',
  'control',
  'human',
  'output',
  'custom',
];

/**
 * Category heading for each group.
 *
 * An explicit map rather than a template string: `MessageKey` is a union of
 * literal keys, so `` `category.${category}` `` would not type-check, and this
 * way a new category is a compile error until it has a heading.
 */
const CATEGORY_KEYS: Record<NodeCategory, MessageKey> = {
  input: 'category.input',
  output: 'category.output',
  model: 'category.model',
  logic: 'category.logic',
  data: 'category.data',
  integration: 'category.integration',
  control: 'category.control',
  human: 'category.human',
  custom: 'category.custom',
};

const CARD_WIDTH = 296;
const CARD_HEIGHT = 170;

/**
 * First free slot on a coarse grid.
 *
 * Clicking the palette repeatedly used to drop nodes at random coordinates, so
 * the third card often half-covered the second. Drag-and-drop still uses the
 * cursor position; this only covers click-to-add.
 */
function freeSlot(nodes: ReadonlyArray<{ position: { x: number; y: number } }>): {
  x: number;
  y: number;
} {
  for (let row = 0; row < 30; row += 1) {
    for (let column = 0; column < 8; column += 1) {
      const x = 80 + column * CARD_WIDTH;
      const y = 80 + row * CARD_HEIGHT;
      const occupied = nodes.some(
        (node) =>
          Math.abs(node.position.x - x) < CARD_WIDTH - 60 &&
          Math.abs(node.position.y - y) < CARD_HEIGHT - 50,
      );
      if (!occupied) return { x, y };
    }
  }
  return { x: 80, y: 80 };
}

export function NodePalette() {
  const t = useT();
  const nodeTypes = useWorkflow((state) => state.nodeTypes);
  const addNodeFromType = useWorkflow((state) => state.addNodeFromType);
  const nodes = useWorkflow((state) => state.nodes);
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const specs = Object.values(nodeTypes).filter((spec) => {
      if (spec.disabled) return false;
      if (!needle) return true;
      return (
        spec.label.toLowerCase().includes(needle) ||
        spec.type.toLowerCase().includes(needle) ||
        spec.description.toLowerCase().includes(needle) ||
        spec.category.toLowerCase().includes(needle)
      );
    });

    const map = new Map<NodeCategory, NodeTypeSpec[]>();
    for (const spec of specs) {
      const list = map.get(spec.category) ?? [];
      list.push(spec);
      map.set(spec.category, list);
    }
    for (const list of map.values()) {
      list.sort((a, b) => a.label.localeCompare(b.label));
    }
    return map;
  }, [nodeTypes, query]);

  const total = useMemo(
    () => [...grouped.values()].reduce((sum, list) => sum + list.length, 0),
    [grouped],
  );

  const toggleCategory = (category: string) => {
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      return next;
    });
  };

  const handleDragStart = (event: React.DragEvent, type: string) => {
    event.dataTransfer.setData('application/scaffold-node', type);
    event.dataTransfer.effectAllowed = 'move';
  };

  return (
    <aside className="palette">
      <div className="palette__search">
        <Search size={13} className="palette__search-icon" />
        <input
          className="input palette__search-input"
          placeholder={t('palette.search')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t('palette.searchLabel')}
        />
      </div>

      <div className="palette__list scroll">
        {total === 0 ? (
          <p className="empty">
            {query.trim() ? t('palette.none', { query }) : t('palette.empty')}
          </p>
        ) : null}

        {CATEGORY_ORDER.filter((category) => grouped.has(category)).map((category) => {
          const specs = grouped.get(category)!;
          const isCollapsed = collapsed.has(category);

          return (
            <section key={category} className="palette__group">
              <button
                type="button"
                className="palette__group-header"
                onClick={() => toggleCategory(category)}
                aria-expanded={!isCollapsed}
              >
                <span className={`palette__chevron ${isCollapsed ? '' : 'is-open'}`}>›</span>
                {t(CATEGORY_KEYS[category])}
                <span className="palette__count">{specs.length}</span>
              </button>

              {!isCollapsed ? (
                <div className="palette__items">
                  {specs.map((spec) => (
                    <PaletteItem
                      key={spec.type}
                      spec={spec}
                      onDragStart={handleDragStart}
                      onAdd={() => addNodeFromType(spec.type, freeSlot(nodes))}
                    />
                  ))}
                </div>
              ) : null}
            </section>
          );
        })}
      </div>

      <footer className="palette__footer">
        <span className="dim">
          {t(total === 1 ? 'palette.count.one' : 'palette.count.many', { count: total })}
        </span>
      </footer>
    </aside>
  );
}

function PaletteItem({
  spec,
  onDragStart,
  onAdd,
}: {
  spec: NodeTypeSpec;
  onDragStart: (event: React.DragEvent, type: string) => void;
  onAdd: () => void;
}) {
  const t = useT();

  return (
    <button
      type="button"
      className="palette__item"
      draggable
      onDragStart={(event) => onDragStart(event, spec.type)}
      onClick={onAdd}
      // Native tooltips are the right amount of affordance here: the palette is
      // dense, and a rich popover would fight the drag interaction.
      title={`${spec.description}\n\n${t('palette.clickHint')}`}
      style={{ '--node-color': spec.color } as React.CSSProperties}
    >
      <span className="palette__item-icon">
        <Icon name={spec.icon} size={13} />
      </span>
      <span className="palette__item-text">
        <span className="palette__item-label">
          {spec.label}
          {spec.requires_trust ? (
            <span className="palette__item-flag" title={t('palette.trust')}>
              code
            </span>
          ) : null}
        </span>
        <span className="palette__item-description">{spec.description}</span>
      </span>
    </button>
  );
}

export { NODE_CATEGORIES };
