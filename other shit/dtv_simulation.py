#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Digital Television Modulation Playground
# Author: Antigravity
# Description: Digital Television Modulation Playground simulating ATSC, DVB-S2, DVB-T, DVB-T2, and J.83B
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from PyQt5.QtCore import QObject, pyqtSlot
from gnuradio import blocks
from gnuradio import channels
from gnuradio.filter import firdes
from gnuradio import digital
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import network
import sip
import threading



class dtv_simulation(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Digital Television Modulation Playground", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Digital Television Modulation Playground")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "dtv_simulation")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.j83b_points = j83b_points = [complex(x, y) for x in [-7,-5,-3,-1,1,3,5,7] for y in [-7,-5,-3,-1,1,3,5,7]]
        self.dvbt2_points = dvbt2_points = [complex(x, y) * (0.8746 + 0.4848j) for x in [-7,-5,-3,-1,1,3,5,7] for y in [-7,-5,-3,-1,1,3,5,7]]
        self.atsc_points = atsc_points = [-7, -5, -3, -1, 1, 3, 5, 7]
        self.j83b_const = j83b_const = digital.constellation_calcdist(j83b_points, list(range(64)),
        4, 1, digital.constellation.AMPLITUDE_NORMALIZATION).base()
        self.j83b_const.set_npwr(1.0)
        self.dvbt2_const = dvbt2_const = digital.constellation_calcdist(dvbt2_points, list(range(64)),
        4, 1, digital.constellation.AMPLITUDE_NORMALIZATION).base()
        self.dvbt2_const.set_npwr(1.0)
        self.dvbs2_points = dvbs2_points = [1+0j, 0.707+0.707j, 0+1j, -0.707+0.707j, -1+0j, -0.707-0.707j, 0-1j, 0.707-0.707j]
        self.dvbs2_const = dvbs2_const = digital.constellation_8psk().base()
        self.dvbs2_const.set_npwr(1.0)
        self.atsc_const = atsc_const = digital.constellation_calcdist(atsc_points, [0, 1, 2, 3, 4, 5, 6, 7],
        2, 1, digital.constellation.AMPLITUDE_NORMALIZATION).base()
        self.atsc_const.set_npwr(1.0)
        self.active_standard = active_standard = 0
        self.tx_path_select = tx_path_select = 0 if active_standard < 2 else 1 if active_standard < 4 else 2
        self.timing_offset = timing_offset = 1.0
        self.samp_rate = samp_rate = 500000
        self.noise_level = noise_level = 0.0
        self.multipath_gain = multipath_gain = 0.0
        self.freq_offset = freq_offset = 0.0
        self.const_points_choice = const_points_choice = [atsc_points, dvbs2_points, j83b_points, dvbt2_points][active_standard if active_standard < 4 else 0]
        self.const_choice = const_choice = [atsc_const, dvbs2_const, j83b_const, dvbt2_const][active_standard if active_standard < 4 else 0]

        ##################################################
        # Blocks
        ##################################################

        self._noise_level_range = qtgui.Range(0.0, 0.5, 0.005, 0.0, 200)
        self._noise_level_win = qtgui.RangeWidget(self._noise_level_range, self.set_noise_level, "Channel Noise Voltage", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._noise_level_win)
        self.unpack_8_p2 = blocks.unpack_k_bits_bb(8)
        self.unpack_8_p1 = blocks.unpack_k_bits_bb(8)
        self.unpack_6_p2 = blocks.unpack_k_bits_bb(6)
        self.unpack_3_p1 = blocks.unpack_k_bits_bb(3)
        self.udp_source = network.udp_source(gr.sizeof_char, 1, 5001, 0, 1470, False, False, False)
        self.udp_sink = network.udp_sink(gr.sizeof_char, 1, '127.0.0.1', 5002, 0, 1470, False)
        self.tx_selector = blocks.selector(gr.sizeof_char*1,0,tx_path_select)
        self.tx_selector.set_enabled(True)
        self.tx_rf_selector = blocks.selector(gr.sizeof_gr_complex*1,tx_path_select,0)
        self.tx_rf_selector.set_enabled(True)
        self.stream_to_tagged_p3 = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, 1470, "packet_len")
        self.rx_rf_selector = blocks.selector(gr.sizeof_gr_complex*1,0,tx_path_select)
        self.rx_rf_selector.set_enabled(True)
        self.rx_byte_selector = blocks.selector(gr.sizeof_char*1,tx_path_select,0)
        self.rx_byte_selector.set_enabled(True)
        self.rf_throttle = blocks.throttle( gr.sizeof_gr_complex*1, samp_rate, True, 0 if "auto" == "auto" else max( int(float(0.1) * samp_rate) if "auto" == "time" else int(0.1), 1) )
        self.rf_spectrum = qtgui.freq_sink_c(
            512, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            samp_rate, #bw
            "", #name
            1,
            None # parent
        )
        self.rf_spectrum.set_update_time(0.05)
        self.rf_spectrum.set_y_axis((-140), 10)
        self.rf_spectrum.set_y_label('Relative Gain', 'dB')
        self.rf_spectrum.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.rf_spectrum.enable_autoscale(False)
        self.rf_spectrum.enable_grid(True)
        self.rf_spectrum.set_fft_average(0.2)
        self.rf_spectrum.enable_axis_labels(True)
        self.rf_spectrum.enable_control_panel(False)
        self.rf_spectrum.set_fft_window_normalized(False)



        labels = ['RF Spectrum', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.rf_spectrum.set_line_label(i, "Data {0}".format(i))
            else:
                self.rf_spectrum.set_line_label(i, labels[i])
            self.rf_spectrum.set_line_width(i, widths[i])
            self.rf_spectrum.set_line_color(i, colors[i])
            self.rf_spectrum.set_line_alpha(i, alphas[i])

        self._rf_spectrum_win = sip.wrapinstance(self.rf_spectrum.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._rf_spectrum_win)
        self.pack_8_p2 = blocks.pack_k_bits_bb(8)
        self.pack_8_p1 = blocks.pack_k_bits_bb(8)
        self.pack_6_p2 = blocks.pack_k_bits_bb(6)
        self.pack_3_p1 = blocks.pack_k_bits_bb(3)
        self.ofdm_tx_p3 = digital.ofdm_tx(
            fft_len=64,
            cp_len=16,
            packet_length_tag_key="packet_len",
            occupied_carriers=([-26, -25, -24, -23, -22, -20, -19, -18, -17, -16, -15, -14, -13, -12, -11, -10, -9, -8, -6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26],),
            pilot_carriers=((-21, -7, 7, 21),),
            pilot_symbols=((1, 1, 1, -1),) * 127,
            sync_word1=None,
            sync_word2=None,
            bps_header=1,
            bps_payload=2,
            rolloff=0,
            debug_log=False,
            scramble_bits=True)
        self.ofdm_rx_p3 = digital.ofdm_rx(
            fft_len=64, cp_len=16,
            frame_length_tag_key='frame_'+"packet_len",
            packet_length_tag_key="packet_len",
            occupied_carriers=([-26, -25, -24, -23, -22, -20, -19, -18, -17, -16, -15, -14, -13, -12, -11, -10, -9, -8, -6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26],),
            pilot_carriers=((-21, -7, 7, 21),),
            pilot_symbols=((1, 1, 1, -1),) * 127,
            sync_word1=None,
            sync_word2=None,
            bps_header=1,
            bps_payload=2,
            debug_log=False,
            scramble_bits=True)
        self.map_symbols_p2 = digital.chunks_to_symbols_bc(const_points_choice, 1)
        self.map_symbols_p1 = digital.chunks_to_symbols_bc(const_points_choice, 1)
        self.decode_symbols_p2 = digital.constellation_decoder_cb(const_choice)
        self.decode_symbols_p1 = digital.constellation_decoder_cb(const_choice)
        self.channel_model = channels.channel_model(
            noise_voltage=noise_level,
            frequency_offset=freq_offset,
            epsilon=timing_offset,
            taps=[1.0, 0.0, multipath_gain],
            noise_seed=0,
            block_tags=False)
        # Create the options list
        self._active_standard_options = [0, 1, 2, 3, 4]
        # Create the labels list
        self._active_standard_labels = ['ATSC (8VSB / 8-PAM)', 'DVB-S2 (8-PSK)', 'ITU-T J.83B (64-QAM)', 'DVB-T2 (Rotated 64-QAM)', 'DVB-T (OFDM - QPSK)']
        # Create the combo box
        self._active_standard_tool_bar = Qt.QToolBar(self)
        self._active_standard_tool_bar.addWidget(Qt.QLabel("DTV Standard" + ": "))
        self._active_standard_combo_box = Qt.QComboBox()
        self._active_standard_tool_bar.addWidget(self._active_standard_combo_box)
        for _label in self._active_standard_labels: self._active_standard_combo_box.addItem(_label)
        self._active_standard_callback = lambda i: Qt.QMetaObject.invokeMethod(self._active_standard_combo_box, "setCurrentIndex", Qt.Q_ARG("int", self._active_standard_options.index(i)))
        self._active_standard_callback(self.active_standard)
        self._active_standard_combo_box.currentIndexChanged.connect(
            lambda i: self.set_active_standard(self._active_standard_options[i]))
        # Create the radio buttons
        self.top_layout.addWidget(self._active_standard_tool_bar)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.channel_model, 0), (self.rf_spectrum, 0))
        self.connect((self.channel_model, 0), (self.rx_rf_selector, 0))
        self.connect((self.decode_symbols_p1, 0), (self.unpack_3_p1, 0))
        self.connect((self.decode_symbols_p2, 0), (self.unpack_6_p2, 0))
        self.connect((self.map_symbols_p1, 0), (self.tx_rf_selector, 0))
        self.connect((self.map_symbols_p2, 0), (self.tx_rf_selector, 1))
        self.connect((self.ofdm_rx_p3, 0), (self.rx_byte_selector, 2))
        self.connect((self.ofdm_tx_p3, 0), (self.tx_rf_selector, 2))
        self.connect((self.pack_3_p1, 0), (self.map_symbols_p1, 0))
        self.connect((self.pack_6_p2, 0), (self.map_symbols_p2, 0))
        self.connect((self.pack_8_p1, 0), (self.rx_byte_selector, 0))
        self.connect((self.pack_8_p2, 0), (self.rx_byte_selector, 1))
        self.connect((self.rf_throttle, 0), (self.channel_model, 0))
        self.connect((self.rx_byte_selector, 0), (self.udp_sink, 0))
        self.connect((self.rx_rf_selector, 0), (self.decode_symbols_p1, 0))
        self.connect((self.rx_rf_selector, 1), (self.decode_symbols_p2, 0))
        self.connect((self.rx_rf_selector, 2), (self.ofdm_rx_p3, 0))
        self.connect((self.stream_to_tagged_p3, 0), (self.ofdm_tx_p3, 0))
        self.connect((self.tx_rf_selector, 0), (self.rf_throttle, 0))
        self.connect((self.tx_selector, 2), (self.stream_to_tagged_p3, 0))
        self.connect((self.tx_selector, 0), (self.unpack_8_p1, 0))
        self.connect((self.tx_selector, 1), (self.unpack_8_p2, 0))
        self.connect((self.udp_source, 0), (self.tx_selector, 0))
        self.connect((self.unpack_3_p1, 0), (self.pack_8_p1, 0))
        self.connect((self.unpack_6_p2, 0), (self.pack_8_p2, 0))
        self.connect((self.unpack_8_p1, 0), (self.pack_3_p1, 0))
        self.connect((self.unpack_8_p2, 0), (self.pack_6_p2, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "dtv_simulation")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_j83b_points(self):
        return self.j83b_points

    def set_j83b_points(self, j83b_points):
        self.j83b_points = j83b_points
        self.set_const_points_choice([self.atsc_points, self.dvbs2_points, self.j83b_points, self.dvbt2_points][self.active_standard if self.active_standard < 4 else 0])

    def get_dvbt2_points(self):
        return self.dvbt2_points

    def set_dvbt2_points(self, dvbt2_points):
        self.dvbt2_points = dvbt2_points
        self.set_const_points_choice([self.atsc_points, self.dvbs2_points, self.j83b_points, self.dvbt2_points][self.active_standard if self.active_standard < 4 else 0])

    def get_atsc_points(self):
        return self.atsc_points

    def set_atsc_points(self, atsc_points):
        self.atsc_points = atsc_points
        self.set_const_points_choice([self.atsc_points, self.dvbs2_points, self.j83b_points, self.dvbt2_points][self.active_standard if self.active_standard < 4 else 0])

    def get_j83b_const(self):
        return self.j83b_const

    def set_j83b_const(self, j83b_const):
        self.j83b_const = j83b_const
        self.set_const_choice([self.atsc_const, self.dvbs2_const, self.j83b_const, self.dvbt2_const][self.active_standard if self.active_standard < 4 else 0])

    def get_dvbt2_const(self):
        return self.dvbt2_const

    def set_dvbt2_const(self, dvbt2_const):
        self.dvbt2_const = dvbt2_const
        self.set_const_choice([self.atsc_const, self.dvbs2_const, self.j83b_const, self.dvbt2_const][self.active_standard if self.active_standard < 4 else 0])

    def get_dvbs2_points(self):
        return self.dvbs2_points

    def set_dvbs2_points(self, dvbs2_points):
        self.dvbs2_points = dvbs2_points
        self.set_const_points_choice([self.atsc_points, self.dvbs2_points, self.j83b_points, self.dvbt2_points][self.active_standard if self.active_standard < 4 else 0])

    def get_dvbs2_const(self):
        return self.dvbs2_const

    def set_dvbs2_const(self, dvbs2_const):
        self.dvbs2_const = dvbs2_const
        self.set_const_choice([self.atsc_const, self.dvbs2_const, self.j83b_const, self.dvbt2_const][self.active_standard if self.active_standard < 4 else 0])

    def get_atsc_const(self):
        return self.atsc_const

    def set_atsc_const(self, atsc_const):
        self.atsc_const = atsc_const
        self.set_const_choice([self.atsc_const, self.dvbs2_const, self.j83b_const, self.dvbt2_const][self.active_standard if self.active_standard < 4 else 0])

    def get_active_standard(self):
        return self.active_standard

    def set_active_standard(self, active_standard):
        self.active_standard = active_standard
        self._active_standard_callback(self.active_standard)
        self.set_tx_path_select(0 if self.active_standard < 2 else 1 if self.active_standard < 4 else 2)
        self.set_const_points_choice([self.atsc_points, self.dvbs2_points, self.j83b_points, self.dvbt2_points][self.active_standard if self.active_standard < 4 else 0])
        self.set_const_choice([self.atsc_const, self.dvbs2_const, self.j83b_const, self.dvbt2_const][self.active_standard if self.active_standard < 4 else 0])

    def get_tx_path_select(self):
        return self.tx_path_select

    def set_tx_path_select(self, tx_path_select):
        self.tx_path_select = tx_path_select
        self.tx_selector.set_output_index(self.tx_path_select)
        self.tx_rf_selector.set_input_index(self.tx_path_select)
        self.rx_rf_selector.set_output_index(self.tx_path_select)
        self.rx_byte_selector.set_input_index(self.tx_path_select)

    def get_timing_offset(self):
        return self.timing_offset

    def set_timing_offset(self, timing_offset):
        self.timing_offset = timing_offset
        self.channel_model.set_timing_offset(self.timing_offset)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.rf_throttle.set_sample_rate(self.samp_rate)
        self.rf_spectrum.set_frequency_range(0, self.samp_rate)

    def get_noise_level(self):
        return self.noise_level

    def set_noise_level(self, noise_level):
        self.noise_level = noise_level
        self.channel_model.set_noise_voltage(self.noise_level)

    def get_multipath_gain(self):
        return self.multipath_gain

    def set_multipath_gain(self, multipath_gain):
        self.multipath_gain = multipath_gain
        self.channel_model.set_taps([1.0, 0.0, self.multipath_gain])

    def get_freq_offset(self):
        return self.freq_offset

    def set_freq_offset(self, freq_offset):
        self.freq_offset = freq_offset
        self.channel_model.set_frequency_offset(self.freq_offset)

    def get_const_points_choice(self):
        return self.const_points_choice

    def set_const_points_choice(self, const_points_choice):
        self.const_points_choice = const_points_choice
        self.map_symbols_p1.set_symbol_table(self.const_points_choice)
        self.map_symbols_p2.set_symbol_table(self.const_points_choice)

    def get_const_choice(self):
        return self.const_choice

    def set_const_choice(self, const_choice):
        self.const_choice = const_choice
        self.decode_symbols_p1.set_constellation(self.const_choice)
        self.decode_symbols_p2.set_constellation(self.const_choice)




def main(top_block_cls=dtv_simulation, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
