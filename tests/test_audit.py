import json
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, BooleanObject, ByteStringObject, DecodedStreamObject, DictionaryObject, NameObject, NullObject, NumberObject
from pdf_inline_audit import Limits, distribution, isolated_scan, main, sample_estimate, scan_pdf

def make_pdf(path, streams, form_data=None, recursive=False, color_spaces=None, form_color_spaces=None):
    writer = PdfWriter()

    def color_resources(spaces):
        table = DictionaryObject({NameObject(name): writer._add_object(value)
                                  for name, value in spaces.items()})
        return DictionaryObject({NameObject('/ColorSpace'): writer._add_object(table)})

    form_ref = None
    if form_data is not None:
        form = DecodedStreamObject()
        form.set_data(form_data)
        form.update({NameObject('/Subtype'): NameObject('/Form'), NameObject('/BBox'): ArrayObject([NumberObject(v) for v in (0, 0, 10, 10)])})
        form_ref = writer._add_object(form)
        if form_color_spaces is not None:
            form[NameObject('/Resources')] = color_resources(form_color_spaces)
        if recursive:
            form[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Fm'): form_ref})})
    for index, data in enumerate(streams):
        page = writer.add_blank_page(100, 100)
        content = DecodedStreamObject()
        content.set_data(data)
        page[NameObject('/Contents')] = writer._add_object(content)
        if form_ref is not None:
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Fm'): form_ref})})
        if color_spaces is not None:
            page.setdefault(NameObject('/Resources'), DictionaryObject()).update(color_resources(color_spaces[index]))
    writer.write(path)
    return path

def inline(data=b'\x00\x01\x02', settings=b'/W 1 /H 1 /BPC 8 /CS /RGB'):
    return b'BI ' + settings + b' ID ' + data + b' EI\n'
import unittest
import tempfile

class TestAudit(unittest.TestCase):

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.tmp_path = Path(self.scratch.name)

    def test_exact_raw_samples_and_no_false_positive_inside_string(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'sample.pdf', [b'BT (BI /W 9 ID fake EI) Tj ET\n' + inline()])
        result = scan_pdf(path)
        assert result['status'] == 'ok'
        assert result['pages_scanned'] == 1
        assert len(result['images']) == 1
        assert result['images'][0]['parsed_encoded_payload_bytes'] == 3
        assert result['images'][0]['estimated_packed_sample_bytes'] == 3
        assert len(result['sha256']) == 64

    def test_filter_array_measures_compressed_payload_not_pixels(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'filtered.pdf', [inline(b'00>', b'/W 100 /H 100 /BPC 8 /CS /G /F [/AHx /Fl]')])
        result = scan_pdf(path)
        image = result['images'][0]
        assert image['filters'] == ['/AHx', '/Fl']
        assert image['parsed_encoded_payload_bytes'] == 3
        assert image['estimated_packed_sample_bytes'] == 10000

    def test_long_form_keys(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'long.pdf', [inline(b'ff>', b'/Width 1 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceGray /Filter /ASCIIHexDecode')])
        image = scan_pdf(path)['images'][0]
        assert image['parsed_encoded_payload_bytes'] == 3
        assert image['estimated_packed_sample_bytes'] == 1

    def test_reused_form_one_definition_three_invocations(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'form.pdf', [b'/Fm Do /Fm Do', b'/Fm Do'], inline())
        result = scan_pdf(path)
        assert result['status'] == 'ok'
        assert len(result['images']) == 1
        assert result['images'][0]['invocations'] == 3
        assert result['images'][0]['stream'].startswith('form:')

    def test_form_cycle_is_partial_not_hang(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'cycle.pdf', [b'/Fm Do'], inline() + b'/Fm Do', recursive=True)
        result = scan_pdf(path)
        assert result['status'] == 'partial'
        assert 'cyclic_form' in result['errors'][0]
        assert len(result['images']) == 1

    def test_missing_xobject_is_reported(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'missing.pdf', [b'/Missing Do'])
        result = scan_pdf(path)
        assert result['status'] == 'partial'
        assert 'missing_xobject' in result['errors'][0]

    def test_page_limit_does_not_claim_full_coverage(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'pages.pdf', [inline(), inline()])
        result = scan_pdf(path, Limits(max_pages=1))
        assert result['status'] == 'partial'
        assert result['pages_scanned'] == 1
        assert result['pages_total'] == 2
        assert result['errors'] == ['page_count_limit']

    def test_image_limit_retains_partial_measurements(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'images.pdf', [inline() + inline()])
        result = scan_pdf(path, Limits(max_images=1))
        assert result['status'] == 'partial'
        assert len(result['images']) == 1
        assert 'image_count_limit' in result['errors'][0]

    def test_size_limit_skips_file(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'size.pdf', [inline()])
        result = scan_pdf(path, Limits(max_file_bytes=1))
        assert result['status'] == 'skipped'
        assert result['images'] == []

    def test_encrypted_requires_password(self):
        tmp_path = self.tmp_path
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        writer.encrypt('not-empty')
        path = tmp_path / 'encrypted.pdf'
        writer.write(path)
        result = scan_pdf(path)
        assert result['status'] == 'encrypted'

    def test_bad_file_is_explicit_error(self):
        tmp_path = self.tmp_path
        path = tmp_path / 'broken.pdf'
        path.write_bytes(b'not a PDF')
        assert scan_pdf(path)['status'] == 'error'

    def test_row_padding_and_image_masks(self):
        tmp_path = self.tmp_path
        assert sample_estimate({'/W': 9, '/H': 2, '/BPC': 1, '/CS': '/G'}) == (4, None)
        assert sample_estimate({'/W': 9, '/H': 2, '/IM': BooleanObject(True)}) == (4, None)

    def test_false_mask_does_not_turn_rgb_into_one_bit(self):
        tmp_path = self.tmp_path
        assert sample_estimate({'/W': 2, '/H': 2, '/BPC': 8, '/CS': '/RGB', '/IM': BooleanObject(False)}) == (12, None)

    def test_unresolved_colorspace_is_unknown_not_guessed(self):
        tmp_path = self.tmp_path
        estimate, reason = sample_estimate({'/W': 2, '/H': 3, '/BPC': 8, '/CS': '/Custom'})
        assert estimate is None
        assert reason == 'unresolved_or_unsupported_color_space'

    def test_named_indexed_colorspace_resolves_indirect_page_resources(self):
        indexed = ArrayObject([NameObject('/Indexed'), NameObject('/DeviceRGB'),
                               NumberObject(1), ByteStringObject(b'\x00\x00\x00\xff\xff\xff')])
        path = make_pdf(self.tmp_path / 'named-indexed.pdf',
                        [inline(b'00000000>', b'/W 9 /H 2 /BPC 1 /CS /Palette /F /AHx')],
                        color_spaces=[{'/Palette': indexed}])
        result = scan_pdf(path)
        assert result['status'] == 'ok'
        assert result['images'][0]['estimated_packed_sample_bytes'] == 4

    def test_form_colorspace_overrides_page_resource_name(self):
        path = make_pdf(self.tmp_path / 'form-colors.pdf', [b'/Fm Do'],
                        inline(b'000102030405060708090a0b>', b'/W 2 /H 2 /BPC 8 /CS /Custom /F /AHx'),
                        color_spaces=[{'/Custom': NameObject('/DeviceGray')}],
                        form_color_spaces={'/Custom': NameObject('/DeviceRGB')})
        result = scan_pdf(path)
        assert result['status'] == 'ok'
        assert result['images'][0]['estimated_packed_sample_bytes'] == 12

    def test_null_form_resources_match_absence_but_empty_dictionary_does_not(self):
        source = make_pdf(self.tmp_path / 'source.pdf', [b'/Fm Do /Fm Do'],
                          inline(b'00>', b'/W 1 /H 1 /BPC 8 /CS /Custom /F /AHx'),
                          color_spaces=[{'/Custom': NameObject('/DeviceGray')}])
        cases = [
            ('absent', None, False, 1),
            ('direct-null', NullObject(), False, 1),
            ('indirect-null', NullObject(), True, 1),
            ('empty-dictionary', DictionaryObject(), False, None),
        ]
        for name, resources, indirect, expected in cases:
            with self.subTest(resources=name):
                path = self.tmp_path / f'{name}.pdf'
                writer = PdfWriter(clone_from=str(source))
                form = writer.pages[0]['/Resources']['/XObject']['/Fm'].get_object()
                if resources is not None:
                    form[NameObject('/Resources')] = (
                        writer._add_object(resources) if indirect else resources)
                writer.write(path)
                writer.close()
                result = scan_pdf(path)
                assert result['status'] == 'ok'
                assert result['errors'] == []
                assert len(result['images']) == 1
                image = result['images'][0]
                assert image['invocations'] == 2
                assert image['estimated_packed_sample_bytes'] == expected
                reason = 'unresolved_or_unsupported_color_space' if expected is None else None
                assert image['estimate_unknown_reason'] == reason

    def test_reused_form_with_conflicting_inherited_colorspaces_is_unknown(self):
        path = make_pdf(self.tmp_path / 'context-dependent.pdf', [b'/Fm Do', b'/Fm Do'],
                        inline(b'000102>', b'/W 1 /H 1 /BPC 8 /CS /Custom /F /AHx'),
                        color_spaces=[{'/Custom': NameObject('/DeviceGray')},
                                      {'/Custom': NameObject('/DeviceRGB')}])
        result = scan_pdf(path)
        assert result['status'] == 'ok'
        assert len(result['images']) == 1
        image = result['images'][0]
        assert image['invocations'] == 2
        assert image['estimated_packed_sample_bytes'] is None
        assert image['estimate_unknown_reason'] == 'context_dependent_color_space'

    def test_cyclic_colorspace_aliases_remain_unknown(self):
        estimate, reason = sample_estimate(
            {'/W': 2, '/H': 3, '/BPC': 8, '/CS': '/A'},
            {'/ColorSpace': {'/A': '/B', '/B': '/A'}})
        assert estimate is None
        assert reason == 'unresolved_or_unsupported_color_space'

    def test_indexed_colorspace_has_one_component(self):
        tmp_path = self.tmp_path
        assert sample_estimate({'/W': 9, '/H': 2, '/BPC': 1, '/CS': ['/I', '/RGB']}) == (4, None)

    def test_distribution_thresholds_are_strict_and_null_for_empty(self):
        tmp_path = self.tmp_path
        assert distribution([])['maximum'] is None
        values = distribution([1, 4096, 4097, 8192, 8193])
        assert values['greater_than_4096'] == 3
        assert values['greater_than_8192'] == 1
        assert values['p50_nearest_rank'] == 4097

    def test_worker_timeout_terminates_and_cleans_up(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'sample.pdf', [inline()])
        result = isolated_scan(path, Limits(), timeout=1e-06)
        assert result['status'] == 'timeout'

    def test_cli_spawn_json_and_duplicate_inputs(self):
        tmp_path = self.tmp_path
        path = make_pdf(tmp_path / 'sample.PDF', [inline()])
        output = tmp_path / 'report.json'
        assert main([str(tmp_path), str(path), '--output', str(output)]) == 0
        report = json.loads(output.read_text())
        assert len(report['files']) == 1
        assert report['summary']['unique_inline_definitions'] == 1
        assert report['schema_version'] == 1

    def test_cli_partial_exit_status(self):
        tmp_path = self.tmp_path
        path = tmp_path / 'broken.pdf'
        path.write_bytes(b'broken')
        assert main([str(path), '--output', str(tmp_path / 'report.json')]) == 1

    def test_cli_rejects_nonpositive_limits(self):
        tmp_path = self.tmp_path
        for option in ['--timeout', '--max-file-mib', '--max-pages', '--max-images']:
            with self.assertRaises(SystemExit) as raised:
                main([str(tmp_path), '--output', str(tmp_path / 'report.json'), option, '0'])
            assert raised.exception.code == 2

    def test_cli_rejects_empty_directory(self):
        tmp_path = self.tmp_path
        with self.assertRaises(SystemExit):
            main([str(tmp_path), '--output', str(tmp_path / 'report.json')])

    def test_output_cannot_overwrite_input_pdf(self):
        path = make_pdf(self.tmp_path / 'input.pdf', [inline()])
        original = path.read_bytes()
        with self.assertRaises(SystemExit):
            main([str(path), '--output', str(path)])
        assert path.read_bytes() == original

    def test_output_hardlink_to_input_is_rejected(self):
        path = make_pdf(self.tmp_path / 'input.pdf', [inline()])
        alias = self.tmp_path / 'alias.json'
        try:
            alias.hardlink_to(path)
        except OSError as error:
            self.skipTest(str(error))
        original = path.read_bytes()
        with self.assertRaises(SystemExit):
            main([str(path), '--output', str(alias)])
        assert path.read_bytes() == original

    def test_existing_report_is_preserved(self):
        path = make_pdf(self.tmp_path / 'input.pdf', [inline()])
        report = self.tmp_path / 'report.json'
        report.write_text('existing report', encoding='utf-8')
        with self.assertRaises(SystemExit):
            main([str(path), '--output', str(report)])
        assert report.read_text(encoding='utf-8') == 'existing report'

    def test_mask_with_contradictory_bits_is_unknown(self):
        assert sample_estimate({'/W': 2, '/H': 2, '/IM': BooleanObject(True),
                                '/BPC': 8}) == (None, 'contradictory_image_mask_bits')
if __name__ == '__main__':
    unittest.main()
