// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import script from '../../app/static/js/sale-stock-notification.js?raw';

const styles = readFileSync(
  resolve(process.cwd(), '../app/static/css/sale-stock-notification.css'),
  'utf8',
);

declare global {
  interface Window {
    VechasuSaleStockNotification?: {
      show(payload: unknown): HTMLElement | null;
    };
  }
}

describe('automatic sale stock notification', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
    delete window.VechasuSaleStockNotification;
    new Function(script)();
  });

  it('keeps success styling and marks only products that reached zero', () => {
    const card = window.VechasuSaleStockNotification!.show({
      order_number: '21156',
      items: [
        { product_id: '1', name: 'Товар A', stock_before: 7, stock_after: 6 },
        { product_id: '2', name: 'Товар B', stock_before: 1, stock_after: 0 },
        { product_id: '3', name: 'Товар C', stock_before: 1, stock_after: 0 },
      ],
    })!;

    expect(card.querySelector('.sale-stock-toast__icon')?.textContent).toBe('✓');
    expect(card.textContent).toContain('Продажа проведена');
    expect(card.textContent).toContain('Заказ №21156');
    expect(card.querySelectorAll('.sale-stock-toast__item.is-empty')).toHaveLength(2);
    expect(card.querySelectorAll('.sale-stock-toast__warning')).toHaveLength(2);
    expect(card.querySelector('[data-product-id="1"]')?.classList.contains('is-empty')).toBe(false);
  });

  it('pauses auto-close while hovered and supports manual close', () => {
    const card = window.VechasuSaleStockNotification!.show({
      order_number: '1',
      items: [{ product_id: '1', name: 'Товар', stock_before: 7, stock_after: 6 }],
    })!;
    card.dispatchEvent(new MouseEvent('mouseenter'));
    vi.advanceTimersByTime(12000);
    expect(card.isConnected).toBe(true);
    card.dispatchEvent(new MouseEvent('mouseleave'));
    vi.advanceTimersByTime(9000);
    expect(card.isConnected).toBe(false);

    const next = window.VechasuSaleStockNotification!.show({
      order_number: '2',
      items: [{ product_id: '2', name: 'Товар', stock_before: 1, stock_after: 0 }],
    })!;
    next.querySelector<HTMLButtonElement>('.sale-stock-toast__close')!.click();
    expect(next.isConnected).toBe(false);
  });

  it('has bounded desktop height, internal scrolling, and mobile width support', () => {
    expect(styles).toContain('max-height: min(560px, calc(100vh - 92px))');
    expect(styles).toContain('overflow-y: auto');
    expect(styles).toContain('@media (max-width: 600px)');
    expect(styles).toContain('max-height: calc(100vh - 74px)');
  });
});
