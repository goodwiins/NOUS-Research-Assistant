import { describe, expect, it, vi } from 'vitest';

import { generateUsageHeatmap } from '../useAnalytics';

describe('generateUsageHeatmap (GOO-724)', () => {
  it('derives intensity only from the reported peak hours', () => {
    const random = vi.spyOn(Math, 'random');
    const heatmap = generateUsageHeatmap({ peak_usage_hours: [9, 14] });

    expect(random).not.toHaveBeenCalled();
    expect(heatmap).toHaveLength(24);
    expect(heatmap[9]).toEqual({ hour: 9, intensity: 1, label: '9:00' });
    expect(heatmap[14]?.intensity).toBe(1);
    expect(
      heatmap.filter((cell) => cell.intensity !== 0).map((cell) => cell.hour)
    ).toEqual([9, 14]);
    random.mockRestore();
  });

  it('is identical across calls and handles missing data', () => {
    const input = { peak_usage_hours: [3] };
    expect(generateUsageHeatmap(input)).toEqual(generateUsageHeatmap(input));
    expect(
      generateUsageHeatmap(undefined).every((cell) => cell.intensity === 0)
    ).toBe(true);
  });
});
