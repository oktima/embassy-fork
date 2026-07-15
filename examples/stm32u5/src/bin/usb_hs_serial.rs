#![no_std]
#![no_main]

use defmt::{panic, *};
use defmt_rtt as _; // global logger
use embassy_executor::Spawner;
use embassy_futures::join::join;
use embassy_stm32::usb::{Driver, Instance};
use embassy_stm32::{Config, bind_interrupts, peripherals, usb};
use embassy_time::{Duration, Instant};
use embassy_usb::Builder;
use embassy_usb::class::cdc_acm::{CdcAcmClass, State};
use embassy_usb::driver::EndpointError;
use panic_probe as _;

bind_interrupts!(struct Irqs {
    OTG_HS => usb::InterruptHandler<peripherals::USB_OTG_HS>;
});

#[embassy_executor::main]
async fn main(_spawner: Spawner) {
    info!("Hello World!");

    let mut config = Config::default();
    {
        use embassy_stm32::rcc::*;
        use embassy_stm32::time::Hertz;
        config.rcc.hse = Some(Hse {
            freq: Hertz(16_000_000),
            mode: HseMode::Oscillator,
        });
        config.rcc.pll1 = Some(Pll {
            source: PllSource::Hse,
            prediv: PllPreDiv::Div2,   // HSE / 2 = 8MHz
            mul: PllMul::Mul60,        // 8MHz * 60 = 480MHz
            divr: Some(PllDiv::Div3),  // 480MHz / 3 = 160MHz (sys_ck)
            divq: Some(PllDiv::Div10), // 480MHz / 10 = 48MHz (USB)
            divp: Some(PllDiv::Div15), // 480MHz / 15 = 32MHz (USBOTG)
        });
        config.rcc.mux.otghssel = mux::Otghssel::Pll1P;
        config.rcc.voltage_range = VoltageScale::Range1;
        config.rcc.sys = Sysclk::Pll1R;
    }

    let p = embassy_stm32::init(config);
    embassy_stm32::pac::ICACHE.cr().write(|w| {
        w.set_en(true);
    });

    // Create the driver, from the HAL.
    // Sized for the control EP (64+4 bytes) plus a 16-packet bulk OUT ring (16 * (512+4) bytes).
    let mut ep_out_buffer = [0u8; 8448];
    let mut config = embassy_stm32::usb::Config::default();
    // Buffer up to 16 bulk OUT packets in hardware before software must intervene.
    // This lets the host burst packets back-to-back instead of NAK-ing after every packet.
    config.out_burst_packets = 16;
    // Do not enable vbus_detection. This is a safe default that works in all boards.
    // However, if your USB device is self-powered (can stay powered on if USB is unplugged), you need
    // to enable vbus_detection to comply with the USB spec. If you enable it, the board
    // has to support it or USB won't work at all. See docs on `vbus_detection` for details.
    config.vbus_detection = false;
    let driver = Driver::new_hs(p.USB_OTG_HS, Irqs, p.PA12, p.PA11, &mut ep_out_buffer, config);

    // Create embassy-usb Config
    let mut config = embassy_usb::Config::new(0xc0de, 0xcafe);
    config.manufacturer = Some("Embassy");
    config.product = Some("USB-serial example");
    config.serial_number = Some("12345678");

    // Create embassy-usb DeviceBuilder using the driver and config.
    // It needs some buffers for building the descriptors.
    let mut config_descriptor = [0; 256];
    let mut bos_descriptor = [0; 256];
    let mut control_buf = [0; 64];

    let mut state = State::new();

    let mut builder = Builder::new(
        driver,
        config,
        &mut config_descriptor,
        &mut bos_descriptor,
        &mut [], // no msos descriptors
        &mut control_buf,
    );

    // Create classes on the builder.
    // High-speed bulk endpoints must have a max packet size of exactly 512 bytes.
    let mut class = CdcAcmClass::new(&mut builder, &mut state, 512);

    // Build the builder.
    let mut usb = builder.build();

    // Run the USB device.
    let usb_fut = usb.run();

    // Sink all incoming data and report the receive rate over defmt.
    let sink_fut = async {
        loop {
            class.wait_connection().await;
            info!("Connected");
            let _ = sink(&mut class).await;
            info!("Disconnected");
        }
    };

    // Run everything concurrently.
    // If we had made everything `'static` above instead, we could do this using separate tasks instead.
    join(usb_fut, sink_fut).await;
}

struct Disconnected {}

impl From<EndpointError> for Disconnected {
    fn from(val: EndpointError) -> Self {
        match val {
            EndpointError::BufferOverflow => panic!("Buffer overflow"),
            EndpointError::Disabled => Disconnected {},
        }
    }
}

/// Reads and discards all incoming data, logging throughput once per second.
async fn sink<'d, T: Instance + 'd>(class: &mut CdcAcmClass<'d, Driver<'d, T>>) -> Result<(), Disconnected> {
    let mut buf = [0; 512];
    let mut total: u64 = 0;
    let mut window_bytes: u32 = 0;
    let mut window_packets: u32 = 0;
    let mut window_start = Instant::now();
    loop {
        let n = class.read_packet(&mut buf).await?;
        total += n as u64;
        window_bytes += n as u32;
        window_packets += 1;
        // Check the clock only every 64 packets to keep per-packet overhead low.
        if window_packets % 64 == 0 {
            let elapsed = window_start.elapsed();
            if elapsed >= Duration::from_secs(1) {
                let kb_per_s = window_bytes as u64 / elapsed.as_millis() as u64;
                info!(
                    "RX: {} kB/s ({} packets, {} bytes total)",
                    kb_per_s, window_packets, total
                );
                window_bytes = 0;
                window_packets = 0;
                window_start = Instant::now();
            }
        }
    }
}
