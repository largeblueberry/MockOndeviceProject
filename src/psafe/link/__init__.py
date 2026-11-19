"""보드 간 통신 (DESIGN.md §7.4).

    messages.py   Report / Control, encode / decode (순수)
    transport.py  Transport 인터페이스, UdpTransport, LoopbackNetwork (I/O)
    peer.py       PeerTracker: 노드별 중복 제거·재시작·무응답 판정 (순수)
"""
