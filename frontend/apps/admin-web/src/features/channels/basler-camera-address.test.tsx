import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  BaslerCameraIpField,
  baslerCameraAddressConfig,
  validBaslerCameraIp
} from './basler-camera-address';

describe('Basler camera IP configuration', () => {
  it('saves the IP alongside the exact serial number', () => {
    expect(baslerCameraAddressConfig({ serial_number: '41949247' }, ' 172.23.27.243 '))
      .toEqual({ serial_number: '41949247', ip_address: '172.23.27.243' });
  });

  it('can remove a saved fallback while preserving other source settings', () => {
    const payload = {
      fps: 15, gain_db: 26, ip_address: '172.23.27.243',
      ...baslerCameraAddressConfig({ serial_number: '41949247' }, '')
    };
    expect(JSON.parse(JSON.stringify(payload)))
      .toEqual({ serial_number: '41949247', fps: 15, gain_db: 26 });
  });

  it('keeps legacy IP-only identifiers', () => {
    expect(baslerCameraAddressConfig({ ip_address: '172.23.27.243' }, ''))
      .toEqual({ ip_address: '172.23.27.243' });
  });

  it.each(['', '172.23.27.243', ' 192.168.10.42 '])('accepts optional IPv4: %s', (value) => {
    expect(validBaslerCameraIp(value)).toBe(true);
  });

  it.each(['172.23.27.999', 'camera.local', '::1', '0.0.0.0', '224.0.0.1',
    '255.255.255.255', '0172.23.27.243', '172.23.27'])('rejects invalid IP: %s', (value) => {
    expect(validBaslerCameraIp(value)).toBe(false);
  });

  it('renders a separate labelled camera IP and explains serial verification', () => {
    const html = renderToStaticMarkup(
      <BaslerCameraIpField value="172.23.27.243" onChange={() => undefined} />
    );
    expect(html).toContain('Kamera-IP (optional)');
    expect(html).toContain('value="172.23.27.243"');
    expect(html).toContain('Seriennummer');
    expect(html).toContain('aria-invalid="false"');
  });
});
