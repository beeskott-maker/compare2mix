from compare2mix.normalize import artist_key, fold, parse_title, split_artists


def test_fold_accents_and_punctuation():
    assert fold("Café del Mar") == "cafe del mar"
    assert fold("RÜFÜS DU SOL") == "rufus du sol"
    assert fold("Don’t Stop") == "dont stop"
    assert fold("Simon & Garfunkel") == "simon and garfunkel"


def test_split_artists():
    assert split_artists("CamelPhat, Elderbrook") == ["CamelPhat", "Elderbrook"]
    assert split_artists("A & B feat. C") == ["A", "B", "C"]
    assert split_artists("") == []


def test_spotify_suffix_format_is_not_significant():
    p = parse_title("Strobe - Radio Edit")
    assert p.base == "strobe"
    assert p.formats == ["radio edit"]
    assert p.significant == []


def test_original_mix_is_ignored():
    p = parse_title("Strobe", mix="Original Mix")
    assert p.base == "strobe" and p.formats == [] and p.significant == []


def test_remaster_is_format():
    assert parse_title("Song - Remastered 2011").formats == ["remastered 2011"]
    assert parse_title("Song - 2011 Remaster").formats == ["2011 remaster"]


def test_remix_is_significant():
    p = parse_title("Innerbloom - What So Not Remix")
    assert p.base == "innerbloom"
    assert p.significant == ["what so not remix"]


def test_brackets_and_feat():
    p = parse_title("Song (feat. Someone) [Extended Mix]")
    assert p.base == "song"
    assert p.featured == ["someone"]
    assert p.formats == ["extended mix"]


def test_dash_in_real_title_kept():
    assert parse_title("Part 1 - The Beginning").base == "part 1 the beginning"


def test_mix_field_merged_without_duplicates():
    p = parse_title("Cafe del Mar (Three N One Remix)", mix="Three N One Remix")
    assert p.significant == ["three n one remix"]


def test_artist_key_order_independent():
    assert artist_key(["B", "A"]) == artist_key(["A & B"])
